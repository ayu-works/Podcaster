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


def _subscriber_topics(conn, email: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT sub.topic FROM subscriber s "
        "JOIN subscription sub ON sub.subscriber_id=s.id "
        "WHERE s.status='active' AND s.email=? ORDER BY sub.topic",
        (email.strip().lower(),),
    ).fetchall()
    topics = tuple(row[0] for row in rows)
    if not topics:
        raise RuntimeError("short digest requires an active subscriber with topics")
    return topics


def _safe_rollback(conn) -> None:
    """Best-effort cleanup that must not replace the pipeline exception."""
    try:
        conn.rollback()
    except Exception:
        pass


def execute(
    conn,
    *,
    dry_run: bool = False,
    skip_fetch: bool = False,
    skip_tag: bool = False,
    tag_limit: int | None = None,
    delivery_email: str | None = None,
    short_digest: bool = False,
) -> RunMetrics:
    """Execute one run with different failure rules for pipeline and delivery."""
    if os.getenv("GITHUB_ACTIONS") == "true" and not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required in GitHub Actions; local SQLite is ephemeral"
        )
    if short_digest and (skip_fetch or skip_tag or dry_run or not delivery_email):
        raise ValueError(
            "short digest requires a real fetch, tagging, and targeted delivery"
        )

    short_topics: tuple[str, ...] | None = None
    short_episode_ids: list[int] | None = None
    if short_digest:
        short_topics = _subscriber_topics(conn, delivery_email)

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
        print(f"stage={stage} start", flush=True)
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
            fetch_options = {"run_id": run_id}
            if short_digest:
                fetch_options.update(
                    since=fetch.since_timestamp(None),
                    discovery_topics=short_topics,
                    discovery_target=config.SHORT_DISCOVERY_FEED_TARGET,
                    candidate_limit=config.SHORT_EPISODE_LIMIT,
                )
            fetched = fetch.fetch_all(conn, **fetch_options)
            metrics.shows = fetched.shows
            metrics.fetched = fetched.after_filter
            if short_digest:
                short_episode_ids = [row["id"] for row in fetched.candidates]
                if not short_episode_ids:
                    raise RuntimeError(
                        "short digest found no new or untagged episodes to test"
                    )
            metrics.fetch_cutoff_at = conn.execute(
                "SELECT fetch_cutoff_at FROM run WHERE id=?", (run_id,)
            ).fetchone()[0]

        # Fetching performs many writes. Finish that transaction before the
        # slower Groq stage so concurrent writers are not blocked and Turso
        # does not have to retain a transaction stream during model calls.
        conn.commit()
        print(
            f"stage=fetch done shows={metrics.shows} episodes={metrics.fetched}",
            flush=True,
        )

        stage = "tag"
        print(f"stage={stage} start", flush=True)
        if skip_tag:
            metrics.untagged_left, metrics.tag_abandoned = _queue_counts(conn)
        else:
            tag_options = {"limit": tag_limit, "dry_run": dry_run}
            if short_digest:
                tag_options.update(
                    limit=config.SHORT_EPISODE_LIMIT,
                    episode_ids=short_episode_ids,
                    progress=lambda completed, total, current: print(
                        f"stage=tag progress {completed}/{total}", flush=True
                    ),
                    request_progress=lambda batch, attempt: print(
                        f"stage=tag groq-request batch={batch} attempt={attempt}",
                        flush=True,
                    ),
                    request_timeout_seconds=config.SHORT_GROQ_TIMEOUT_SECONDS,
                )
            tagged = tag.tag_all(conn, **tag_options)
            metrics.tagged = tagged.tagged
            metrics.untagged_left = tagged.untagged_left
            metrics.tag_abandoned = tagged.abandoned
            metrics.tokens_used = tagged.tokens_used
            scores = [row[1] for row in tagged.rows]
            metrics.score_p50 = _percentile(scores, 0.50)
            metrics.score_p90 = _percentile(scores, 0.90)
        print(f"stage=tag done tagged={metrics.tagged}", flush=True)

        stage = "curate"
        print(f"stage={stage} start", flush=True)
        if short_digest:
            curated = curate.curate_short(
                conn,
                run_id,
                short_episode_ids or [],
                short_topics or (),
                limit=config.SHORT_EMAIL_LIMIT,
            )
        else:
            curated = curate.curate(
                conn,
                run_id,
                previous_cutoff=previous_cutoff,
            )
        metrics.picks_by_topic = curated.counts_by_topic
        conn.commit()
        print(
            f"stage=curate done picks={sum(curated.counts_by_topic.values())}",
            flush=True,
        )

        stage = "send"
        print(f"stage={stage} start", flush=True)
        subscriber_sql = "SELECT COUNT(*) FROM subscriber WHERE status='active'"
        subscriber_parameters = ()
        if delivery_email:
            subscriber_sql += " AND email=?"
            subscriber_parameters = (delivery_email.strip().lower(),)
        metrics.subscribers = conn.execute(
            subscriber_sql, subscriber_parameters
        ).fetchone()[0]
        delivery_options = {"dry_run": dry_run, "email": delivery_email}
        if short_digest:
            delivery_options["max_picks"] = config.SHORT_EMAIL_LIMIT
            delivery_options["min_picks"] = config.SHORT_EMAIL_LIMIT
        delivered = email_out.deliver_all(conn, run_id, **delivery_options)
        metrics.emails_sent = delivered.sent
        metrics.emails_failed = delivered.failed
        metrics.status = "partial" if delivered.failed else "ok"
        if short_digest and metrics.emails_sent != 1:
            raise RuntimeError(
                "short digest did not send exactly one email; no qualifying picks "
                "or delivery failed"
            )
        print(f"stage=send done emails={metrics.emails_sent}", flush=True)

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
        _safe_rollback(conn)
        metrics.status = "failed"
        metrics.failed_stage = stage
        if mutable_status:
            try:
                db.ensure_connection(conn)
                conn.execute(
                    "UPDATE run SET fetched=?, tagged=?, status='failed', "
                    "finished_at=datetime('now') WHERE id=?",
                    (metrics.fetched, metrics.tagged, run_id),
                )
                conn.commit()
            except Exception:
                _safe_rollback(conn)
        try:
            metrics.untagged_left, metrics.tag_abandoned = _queue_counts(conn)
        except Exception:
            pass
        try:
            _append_log(metrics)
        except Exception:
            pass
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
        "--short-digest",
        action="store_true",
        help="real bounded smoke: 30 feeds, 10 episodes, at most 2 emailed picks",
    )
    parser.add_argument(
        "--email",
        help="deliver only to this active subscriber (manual test safety)",
    )
    args = parser.parse_args()
    if args.tag_limit is not None and args.tag_limit < 1:
        parser.error("--tag-limit must be positive")

    if not args.short_digest:
        db.init_db()
    with db.session() as conn:
        metrics = execute(
            conn,
            dry_run=args.dry_run,
            skip_fetch=args.skip_fetch,
            skip_tag=args.skip_tag,
            tag_limit=args.tag_limit,
            delivery_email=args.email,
            short_digest=args.short_digest,
        )
    print(json.dumps(asdict(metrics), sort_keys=True))
    return 1 if metrics.status in ("failed", "partial") else 0


if __name__ == "__main__":
    raise SystemExit(main())
