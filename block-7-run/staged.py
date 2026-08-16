"""Small, restartable commands for the hosted digest workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _shared import config, db
import curate
import email_out
import fetch
import tag

MAX_TAG_LIMIT = 100


def _run(conn, run_id: int):
    row = conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"run {run_id} does not exist")
    return row


def _mark_failed(conn, run_id: int) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        db.ensure_connection(conn)
        conn.execute(
            "UPDATE run SET status='failed', finished_at=datetime('now') "
            "WHERE id=? AND status='running'",
            (run_id,),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def check_stage(conn) -> dict:
    db.ensure_connection(conn)
    required = {"run", "episode", "subscriber", "subscription", "daily_pick", "sent"}
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(f"database schema missing tables: {missing}")
    return {"database": "ok", "tables_checked": len(required)}


def start_stage(conn) -> dict:
    run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
    conn.commit()
    return {"run_id": run_id, "status": "running"}


def fetch_stage(conn, run_id: int) -> dict:
    _run(conn, run_id)
    try:
        result = fetch.fetch_all(
            conn,
            run_id=run_id,
            progress=lambda completed, total, raw, failed: print(
                f"fetch progress feeds={completed}/{total} raw={raw} failed={failed}",
                flush=True,
            ),
        )
        conn.execute(
            "UPDATE run SET fetched=? WHERE id=?",
            (result.after_filter, run_id),
        )
        conn.commit()
        return {
            "run_id": run_id,
            "shows": result.shows,
            "episodes": result.after_filter,
        }
    except Exception:
        _mark_failed(conn, run_id)
        raise


def tag_stage(conn, run_id: int, limit: int = MAX_TAG_LIMIT) -> dict:
    row = _run(conn, run_id)
    if row["status"] != "running" or not row["fetch_cutoff_at"]:
        raise RuntimeError(f"run {run_id} is not ready for tagging")
    if not 1 <= limit <= MAX_TAG_LIMIT:
        raise ValueError(f"tag limit must be between 1 and {MAX_TAG_LIMIT}")
    try:
        result = tag.tag_all(
            conn,
            limit=limit,
            progress=lambda completed, total, current: print(
                f"tag progress episodes={completed}/{total} "
                f"tagged={current.tagged} tokens={current.tokens_used}",
                flush=True,
            ),
        )
        conn.execute("UPDATE run SET tagged=? WHERE id=?", (result.tagged, run_id))
        conn.commit()
        return {
            "run_id": run_id,
            "selected": result.selected,
            "tagged": result.tagged,
            "tokens": result.tokens_used,
            "untagged_left": result.untagged_left,
        }
    except Exception:
        _mark_failed(conn, run_id)
        raise


def curate_stage(conn, run_id: int) -> dict:
    row = _run(conn, run_id)
    if row["status"] != "running":
        raise RuntimeError(f"run {run_id} is not ready for curation")
    previous = conn.execute(
        "SELECT MAX(fetch_cutoff_at) FROM run "
        "WHERE status IN ('ok', 'partial') AND id<>?",
        (run_id,),
    ).fetchone()[0]
    try:
        result = curate.curate(conn, run_id, previous_cutoff=previous)
        conn.commit()
        return {"run_id": run_id, "picks": result.total}
    except Exception:
        _mark_failed(conn, run_id)
        raise


def send_stage(conn, run_id: int, email: str | None = None) -> dict:
    row = _run(conn, run_id)
    if row["status"] != "running":
        raise RuntimeError(f"run {run_id} is not ready for delivery")
    result = email_out.deliver_all(conn, run_id, email=email)
    status = "partial" if result.failed else "ok"
    conn.execute(
        "UPDATE run SET emails_sent=?, emails_failed=?, status=?, "
        "finished_at=datetime('now') WHERE id=?",
        (result.sent, result.failed, status, run_id),
    )
    conn.commit()
    return {
        "run_id": run_id,
        "sent": result.sent,
        "failed": result.failed,
        "skipped": result.skipped,
        "status": status,
    }


def _write_github_output(path: str | None, values: dict) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="stage", required=True)
    subparsers.add_parser("check")
    start = subparsers.add_parser("start")
    start.add_argument("--github-output")
    for name in ("fetch", "curate", "fail"):
        command = subparsers.add_parser(name)
        command.add_argument("--run-id", type=int, required=True)
    tagging = subparsers.add_parser("tag")
    tagging.add_argument("--run-id", type=int, required=True)
    tagging.add_argument("--limit", type=int, default=MAX_TAG_LIMIT)
    sending = subparsers.add_parser("send")
    sending.add_argument("--run-id", type=int, required=True)
    sending.add_argument("--email")
    args = parser.parse_args()

    with db.session() as conn:
        if args.stage == "check":
            result = check_stage(conn)
        elif args.stage == "start":
            result = start_stage(conn)
            _write_github_output(args.github_output, result)
        elif args.stage == "fetch":
            result = fetch_stage(conn, args.run_id)
        elif args.stage == "tag":
            result = tag_stage(conn, args.run_id, args.limit)
        elif args.stage == "curate":
            result = curate_stage(conn, args.run_id)
        elif args.stage == "send":
            result = send_stage(conn, args.run_id, args.email)
        else:
            _mark_failed(conn, args.run_id)
            result = {"run_id": args.run_id, "status": "failed"}

    print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if result.get("status") == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
