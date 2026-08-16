"""Deterministic gates for per-topic SQL curation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-5-tag"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import curate  # noqa: E402
import db  # noqa: E402


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
LOWER = "2026-08-15 00:00:00"


class CurateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "curate.db"
        db.init_db(self.path)
        with db.session(self.path) as conn:
            self.run_id = conn.execute(
                "INSERT INTO run (fetch_cutoff_at) VALUES ('2026-08-16 10:00:00')"
            ).lastrowid

    def tearDown(self):
        self.temp.cleanup()

    def add_episode(
        self,
        conn,
        guid: str,
        score: int,
        *,
        feed_id: int = 1,
        topics=("technology-ai",),
        tagged_at: str = "2026-08-16 11:00:00",
        published_at: str = "2026-08-16 09:00:00",
    ) -> int:
        episode_id = conn.execute(
            "INSERT INTO episode "
            "(guid, feed_id, show_name, title, description, score, why, tagged_at, published_at) "
            "VALUES (?, ?, 'Show', ?, 'description', ?, 'specific reason', ?, ?)",
            (guid, feed_id, guid, score, tagged_at, published_at),
        ).lastrowid
        conn.executemany(
            "INSERT INTO episode_topic (episode_id, topic) VALUES (?, ?)",
            [(episode_id, topic) for topic in topics],
        )
        return episode_id

    def test_score_order_topic_cap_and_per_show_cap(self):
        with db.session(self.path) as conn:
            self.add_episode(conn, "below", 69, feed_id=50)
            for index in range(15):
                self.add_episode(
                    conn,
                    f"episode-{index}",
                    100 - index,
                    feed_id=1 if index < 5 else index,
                    published_at=f"2026-08-16 {9 - index // 10:02}:00:00",
                )
            result = curate.curate(conn, self.run_id, LOWER, NOW)
            rows = conn.execute(
                "SELECT dp.rank, e.score, e.feed_id FROM daily_pick dp "
                "JOIN episode e ON e.id=dp.episode_id "
                "WHERE dp.run_id=? AND dp.topic='technology-ai' ORDER BY dp.rank",
                (self.run_id,),
            ).fetchall()
        self.assertEqual(len(rows), config.PICKS_PER_TOPIC)
        self.assertTrue(all(row["score"] >= config.RELEVANCE_BAR for row in rows))
        self.assertEqual([row["rank"] for row in rows], list(range(1, 11)))
        self.assertLessEqual(sum(row["feed_id"] == 1 for row in rows), config.CURATE_MAX_PER_SHOW)
        self.assertGreater(result.dropped_same_show, 0)

    def test_empty_topic_is_not_backfilled(self):
        with db.session(self.path) as conn:
            self.add_episode(conn, "low", 10, topics=("travel",))
            result = curate.curate(conn, self.run_id, LOWER, NOW)
        self.assertEqual(result.counts_by_topic["travel"], 0)

    def test_late_tagged_is_eligible_but_stale_publication_is_not(self):
        with db.session(self.path) as conn:
            late_id = self.add_episode(
                conn,
                "late-retry",
                90,
                tagged_at="2026-08-16 11:30:00",
                published_at="2026-08-14 09:00:00",
            )
            stale_id = self.add_episode(
                conn,
                "stale",
                95,
                tagged_at="2026-08-16 11:30:00",
                published_at="2026-08-01 09:00:00",
            )
            curate.curate(conn, self.run_id, LOWER, NOW)
            picked = {
                row[0]
                for row in conn.execute(
                    "SELECT episode_id FROM daily_pick WHERE run_id=?", (self.run_id,)
                ).fetchall()
            }
        self.assertIn(late_id, picked)
        self.assertNotIn(stale_id, picked)

    def test_cross_topic_episode_appears_in_both_lists(self):
        with db.session(self.path) as conn:
            episode_id = self.add_episode(
                conn, "cross", 90, topics=("technology-ai", "science")
            )
            curate.curate(conn, self.run_id, LOWER, NOW)
            topics = conn.execute(
                "SELECT topic FROM daily_pick WHERE run_id=? AND episode_id=? ORDER BY topic",
                (self.run_id, episode_id),
            ).fetchall()
        self.assertEqual([row[0] for row in topics], ["science", "technology-ai"])

    def test_episode_gets_one_editorial_shot_and_same_run_is_idempotent(self):
        with db.session(self.path) as conn:
            episode_id = self.add_episode(conn, "once", 90)
            curate.curate(conn, self.run_id, LOWER, NOW)
            curate.curate(conn, self.run_id, LOWER, NOW)
            current_count = conn.execute(
                "SELECT COUNT(*) FROM daily_pick WHERE run_id=? AND episode_id=?",
                (self.run_id, episode_id),
            ).fetchone()[0]
            next_run = conn.execute(
                "INSERT INTO run (fetch_cutoff_at) VALUES ('2026-08-17 10:00:00')"
            ).lastrowid
            next_result = curate.curate(
                conn, next_run, "2026-08-16 10:00:00", NOW
            )
        self.assertEqual(current_count, 1)
        self.assertEqual(next_result.counts_by_topic["technology-ai"], 0)

    def test_short_curator_selects_two_unique_subscriber_topic_episodes(self):
        with db.session(self.path) as conn:
            first = self.add_episode(
                conn,
                "first",
                40,
                feed_id=1,
                topics=("design", "science"),
            )
            second = self.add_episode(
                conn,
                "second",
                30,
                feed_id=2,
                topics=("history",),
            )
            unrelated = self.add_episode(
                conn,
                "unrelated",
                100,
                feed_id=3,
                topics=("finance",),
            )
            result = curate.curate_short(
                conn,
                self.run_id,
                [first, second, unrelated],
                ("design", "science", "history"),
                limit=2,
                now=NOW,
            )
            rows = conn.execute(
                "SELECT episode_id FROM daily_pick WHERE run_id=? ORDER BY rank",
                (self.run_id,),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [first, second])
        self.assertEqual(result.total, 2)


if __name__ == "__main__":
    unittest.main()
