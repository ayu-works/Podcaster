"""Curate newly tagged episodes into per-topic lists with no AI calls."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from _shared import config, db


class CurateError(RuntimeError):
    """Curation could not safely produce rows for the requested run."""


@dataclass
class CurateResult:
    run_id: int
    counts_by_topic: dict[str, int] = field(default_factory=dict)
    dropped_same_show: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts_by_topic.values())


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def curate(
    conn,
    run_id: int,
    previous_cutoff: str | None = None,
    now: datetime | None = None,
    eligible_episode_ids: list[int] | None = None,
    min_score: int | None = None,
) -> CurateResult:
    """Write at most PICKS_PER_TOPIC, capped per show, for every topic.

    `tagged_at` supplies eligibility and `published_at` only supplies the
    staleness floor. The NOT EXISTS guard gives every episode one editorial
    shot even though a tag timestamp can remain newer than the next run's
    fetch cutoff.
    """
    if conn.execute("SELECT 1 FROM run WHERE id = ?", (run_id,)).fetchone() is None:
        raise CurateError(f"No run with id {run_id}")

    clock = now or datetime.now(timezone.utc)
    lower = previous_cutoff or db.last_good_cutoff(conn)
    if lower is None:
        lower = _timestamp(clock - timedelta(days=config.MAX_LOOKBACK_DAYS))
    staleness_floor = _timestamp(
        clock - timedelta(days=config.CURATE_MAX_AGE_DAYS)
    )

    # Re-running curation for the same run must replace, not append to, its
    # editorial output. Prior runs still exclude an episode via NOT EXISTS.
    conn.execute("DELETE FROM daily_pick WHERE run_id = ?", (run_id,))
    result = CurateResult(run_id=run_id)
    score_floor = config.RELEVANCE_BAR if min_score is None else min_score
    eligible_clause = ""
    eligible_parameters: list[int] = []
    if eligible_episode_ids is not None:
        if not eligible_episode_ids:
            return result
        placeholders = ",".join("?" for _ in eligible_episode_ids)
        eligible_clause = f" AND e.id IN ({placeholders})"
        eligible_parameters = eligible_episode_ids

    for topic in config.TOPIC_SLUGS:
        rows = conn.execute(
            f"""
            SELECT e.*
            FROM episode e
            JOIN episode_topic t ON t.episode_id = e.id
            WHERE t.topic = ?
              AND e.tagged_at IS NOT NULL
              AND e.score >= ?
              AND e.tagged_at > ?
              AND e.published_at > ?
              AND NOT EXISTS (
                  SELECT 1 FROM daily_pick prior
                  WHERE prior.episode_id = e.id
                    AND prior.run_id <> ?
              )
              {eligible_clause}
            ORDER BY e.score DESC, e.published_at DESC
            """,
            (topic, score_floor, lower, staleness_floor, run_id, *eligible_parameters),
        ).fetchall()

        show_counts: Counter = Counter()
        selected = []
        for row in rows:
            if show_counts[row["feed_id"]] >= config.CURATE_MAX_PER_SHOW:
                result.dropped_same_show += 1
                continue
            show_counts[row["feed_id"]] += 1
            selected.append(row)
            if len(selected) == config.PICKS_PER_TOPIC:
                break

        conn.executemany(
            "INSERT INTO daily_pick (run_id, topic, episode_id, rank) "
            "VALUES (?, ?, ?, ?)",
            [
                (run_id, topic, row["id"], rank)
                for rank, row in enumerate(selected, 1)
            ],
        )
        result.counts_by_topic[topic] = len(selected)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-id", type=int)
    args = parser.parse_args()

    with db.session() as conn:
        run_id = args.run_id
        if run_id is None:
            row = conn.execute(
                "SELECT id FROM run WHERE fetch_cutoff_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise CurateError("No fetched run exists")
            run_id = row["id"]
        result = curate(conn, run_id)

    print(f"run {result.run_id}: {result.total} picks")
    for topic, count in result.counts_by_topic.items():
        print(f"  {topic:<24} {count}")
    if result.dropped_same_show:
        print(f"dropped {result.dropped_same_show} rows at the per-show cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
