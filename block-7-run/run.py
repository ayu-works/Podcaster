"""Run dynamic discovery -> fetch -> tag -> curate -> per-user delivery."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from _shared import config, db
import curate
import email_out
import fetch
import tag


@dataclass
class RunMetrics:
    run_id: int
    ran_at: str
    fetch_cutoff_at: str | None = None
    shows: int = 0
    fetched: int = 0
    tagged: int = 0
    untagged_left: int = 0
    tag_abandoned: int = 0
    tokens_used: int = 0
    score_p50: int | None = None
    score_p90: int | None = None
    picks_by_topic: dict[str, int] = field(default_factory=dict)
    subscribers: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    status: str = "running"
    failed_stage: str | None = None


def _percentile(values: list[int], percentile: float) -> int | None:
    """Nearest-rank percentile; stable and meaningful even for one score."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int((len(ordered) * percentile) + 0.999999) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _append_log(metrics: RunMetrics) -> None:
    config.RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.RUN_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(asdict(metrics), sort_keys=True) + "\n")


def _latest_fetched_run(conn):
    return conn.execute(
        "SELECT id, status, fetch_cutoff_at FROM run "
        "WHERE fetch_cutoff_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _previous_cutoff(conn, run_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(fetch_cutoff_at) AS cutoff FROM run "
        "WHERE status IN ('ok', 'partial') AND id <> ?",
        (run_id,),
    ).fetchone()
    return row["cutoff"] if row else None


def _queue_counts(conn) -> tuple[int, int]:
    untagged = conn.execute(
        "SELECT COUNT(*) FROM episode WHERE tagged_at IS NULL AND tag_attempts < ?",
        (config.TAG_MAX_ATTEMPTS,),
    ).fetchone()[0]
    abandoned = conn.execute(
        "SELECT COUNT(*) FROM episode WHERE tagged_at IS NULL AND tag_attempts >= ?",
        (config.TAG_MAX_ATTEMPTS,),
    ).fetchone()[0]
    return untagged, abandoned


def execute(
    conn,
    *,
    dry_run: bool = False,
    skip_fetch: bool = False,
    skip_tag: bool = False,
    tag_limit: int | None = None,
    delivery_email: str | None = None,
) -> RunMetrics:
    """Execute one run with different failure rules for pipeline and delivery."""
    if os.getenv("GITHUB_ACTIONS") == "true" and not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required in GitHub Actions; local SQLite is ephemeral"
        )

    if skip_fetch:
        existing = _latest_fetched_run(conn)
        if existing is None:
            raise RuntimeError("--skip-fetch requires an existing fetched run")
        run_id = existing["id"]
        previous_cutoff = _previous_cutoff(conn, run_id)
        # A failed/running row is a recovery attempt. Preserve a completed
        # row's good status while previewing or iterating on later stages.
        mutable_status = existing["status"] not in ("ok", "partial")
        if mutable_status:
            conn.execute(
                "UPDATE run SET status='running', finished_at=NULL WHERE id=?",
                (run_id,),
            )
    else:
        run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
        previous_cutoff = db.last_good_cutoff(conn)
        mutable_status = True
    conn.commit()

    metrics = RunMetrics(
        run_id=run_id,
        ran_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    stage = "fetch"
    try:
        if skip_fetch:
            row = conn.execute(
                "SELECT fetch_cutoff_at, fetched FROM run WHERE id=?", (run_id,)
            ).fetchone()
            metrics.fetch_cutoff_at = row["fetch_cutoff_at"]
            metrics.fetched = row["fetched"] or 0
            metrics.shows = conn.execute(
                "SELECT COUNT(*) FROM show WHERE status='active'"
            ).fetchone()[0]
        else:
            fetched = fetch.fetch_all(conn, run_id=run_id)
            metrics.shows = fetched.shows
            metrics.fetched = fetched.after_filter
            metrics.fetch_cutoff_at = conn.execute(
                "SELECT fetch_cutoff_at FROM run WHERE id=?", (run_id,)
            ).fetchone()[0]

        stage = "tag"
        if skip_tag:
            metrics.untagged_left, metrics.tag_abandoned = _queue_counts(conn)
        else:
            tagged = tag.tag_all(conn, limit=tag_limit, dry_run=dry_run)
            metrics.tagged = tagged.tagged
            metrics.untagged_left = tagged.untagged_left
            metrics.tag_abandoned = tagged.abandoned
            metrics.tokens_used = tagged.tokens_used
            scores = [row[1] for row in tagged.rows]
            metrics.score_p50 = _percentile(scores, 0.50)
            metrics.score_p90 = _percentile(scores, 0.90)

        stage = "curate"
        curated = curate.curate(conn, run_id, previous_cutoff=previous_cutoff)
        metrics.picks_by_topic = curated.counts_by_topic
        conn.commit()

        stage = "send"
        subscriber_sql = "SELECT COUNT(*) FROM subscriber WHERE status='active'"
        subscriber_parameters = ()
        if delivery_email:
            subscriber_sql += " AND email=?"
            subscriber_parameters = (delivery_email.strip().lower(),)
        metrics.subscribers = conn.execute(
            subscriber_sql, subscriber_parameters
        ).fetchone()[0]
        delivered = email_out.deliver_all(
            conn, run_id, dry_run=dry_run, email=delivery_email
        )
        metrics.emails_sent = delivered.sent
        metrics.emails_failed = delivered.failed
        metrics.status = "partial" if delivered.failed else "ok"

        if mutable_status:
            conn.execute(
                "UPDATE run SET fetched=?, tagged=?, emails_sent=?, emails_failed=?, "
                "status=?, finished_at=datetime('now') WHERE id=?",
                (
                    metrics.fetched,
                    metrics.tagged,
                    metrics.emails_sent,
                    metrics.emails_failed,
                    metrics.status,
                    run_id,
                ),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        metrics.status = "failed"
        metrics.failed_stage = stage
        if mutable_status:
            conn.execute(
                "UPDATE run SET fetched=?, tagged=?, status='failed', "
                "finished_at=datetime('now') WHERE id=?",
                (metrics.fetched, metrics.tagged, run_id),
            )
            conn.commit()
        metrics.untagged_left, metrics.tag_abandoned = _queue_counts(conn)
        _append_log(metrics)
        raise

    _append_log(metrics)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-tag", action="store_true")
    parser.add_argument(
        "--tag-limit",
        type=int,
        help="tag at most this many newest queued episodes (manual smoke tests)",
    )
    parser.add_argument(
        "--email",
        help="deliver only to this active subscriber (manual test safety)",
    )
    args = parser.parse_args()
    if args.tag_limit is not None and args.tag_limit < 1:
        parser.error("--tag-limit must be positive")

    db.init_db()
    with db.session() as conn:
        metrics = execute(
            conn,
            dry_run=args.dry_run,
            skip_fetch=args.skip_fetch,
            skip_tag=args.skip_tag,
            tag_limit=args.tag_limit,
            delivery_email=args.email,
        )
    print(json.dumps(asdict(metrics), sort_keys=True))
    return 1 if metrics.status in ("failed", "partial") else 0


if __name__ == "__main__":
    raise SystemExit(main())
