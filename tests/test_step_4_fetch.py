"""Deterministic gates for implementation-plan Step 4."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-4-fetch"))
sys.path.insert(0, str(ROOT / "block-2-universe"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import fetch  # noqa: E402


def episode(
    guid: str,
    *,
    description_length: int = 100,
    duration: int | None = 180,
    published_at: str = "2026-01-01T01:15:00+00:00",
    title: str = "Episode",
) -> fetch.NewEpisode:
    return fetch.NewEpisode(
        guid=guid,
        feed_id=1,
        show_name="Show",
        title=title,
        description="x" * description_length,
        duration_sec=duration,
        published_at=published_at,
        web_url="https://example.com/episode",
    )


class FetchTests(unittest.TestCase):
    def make_db(self, temp_dir: str) -> Path:
        path = Path(temp_dir) / "fetch.db"
        db.init_db(path)
        with db.session(path) as conn:
            conn.execute(
                "INSERT INTO show (feed_id, feed_url, title) VALUES (1, ?, 'Show')",
                ("https://example.com/feed.xml",),
            )
        return path

    def test_filter_boundaries_and_guid_dedupe(self):
        candidates = [
            episode("thin", description_length=99),
            episode("short", duration=179),
            episode("kept", description_length=100, duration=180),
            episode("kept", description_length=100, duration=180),
            episode("unknown-duration", duration=None),
        ]
        kept, dropped = fetch.filter_episodes(candidates)
        self.assertEqual([item.guid for item in kept], ["kept", "unknown-duration"])
        self.assertEqual(dropped["description under 100 chars"], 1)
        self.assertEqual(dropped["under 3 minutes"], 1)
        self.assertEqual(dropped["duplicate guid"], 1)

    def test_clean_text_and_trailer_mapping(self):
        self.assertEqual(fetch.clean_text("<p>A &amp; B</p>\n C"), "A & B C")
        item = {
            "guid": "trailer",
            "feedId": 1,
            "episodeType": "trailer",
            "description": "x" * 100,
        }
        self.assertIsNone(fetch.to_episode(item, "Show"))

    def test_cutoff_is_committed_before_poll_and_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            seen_since: list[int] = []

            def fake_fetch(shows, since, progress=None):
                seen_since.append(since)
                with db.session(path) as observer:
                    row = observer.execute(
                        "SELECT fetch_cutoff_at FROM run ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    self.assertIsNotNone(row["fetch_cutoff_at"])
                return [episode("guid-1")], 1, 0

            with patch.object(fetch, "fetch_feeds", side_effect=fake_fetch):
                with db.session(path) as conn:
                    first_run = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                    first = fetch.fetch_all(
                        conn, run_id=first_run, refresh_discovery=False
                    )
                    conn.execute(
                        "UPDATE run SET status='ok', finished_at=datetime('now') WHERE id=?",
                        (first_run,),
                    )
                with db.session(path) as conn:
                    second_run = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                    second = fetch.fetch_all(
                        conn, run_id=second_run, refresh_discovery=False
                    )

            self.assertEqual(first.stored, 1)
            self.assertEqual(second.stored, 0)
            self.assertGreater(seen_since[1], seen_since[0])
            with db.session(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0], 1)

    def test_mid_run_publication_is_inside_next_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
            finished = cutoff + timedelta(minutes=30)
            published = cutoff + timedelta(minutes=15)
            with db.session(path) as conn:
                conn.execute(
                    "INSERT INTO run (fetch_cutoff_at, finished_at, status) VALUES (?, ?, 'ok')",
                    (
                        cutoff.strftime("%Y-%m-%d %H:%M:%S"),
                        finished.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid

                def fake_fetch(shows, since, progress=None):
                    self.assertEqual(since, int(cutoff.timestamp()))
                    return [episode("mid-run", published_at=published.isoformat())], 1, 0

                with patch.object(fetch, "fetch_feeds", side_effect=fake_fetch):
                    result = fetch.fetch_all(
                        conn, run_id=run_id, refresh_discovery=False
                    )
            self.assertEqual(result.stored, 1)

    def test_short_fetch_persists_at_most_ten_filtered_episodes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            episodes = [episode(f"bounded-{index}") for index in range(25)]
            with patch.object(fetch, "fetch_feeds", return_value=(episodes, 25, 0)):
                with db.session(path) as conn:
                    run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                    result = fetch.fetch_all(
                        conn,
                        since=0,
                        run_id=run_id,
                        refresh_discovery=False,
                        candidate_limit=10,
                    )
                    stored = conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
        self.assertEqual((result.after_filter, result.stored, stored), (10, 10, 10))
        self.assertEqual(result.dropped["short digest episode cap"], 15)

    def test_short_fetch_skips_already_tagged_rows_before_applying_cap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            tagged = episode("already-tagged", published_at="2026-01-02T00:00:00+00:00")
            fresh = episode("fresh", published_at="2026-01-01T00:00:00+00:00")
            with db.session(path) as conn:
                tagged_id = fetch.upsert_episodes(conn, [tagged])[0]
                conn.execute(
                    "UPDATE episode SET tagged_at=datetime('now'), score=90 WHERE id=?",
                    (tagged_id,),
                )
            with patch.object(fetch, "fetch_feeds", return_value=([tagged, fresh], 2, 0)):
                with db.session(path) as conn:
                    run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                    result = fetch.fetch_all(
                        conn,
                        since=0,
                        run_id=run_id,
                        refresh_discovery=False,
                        candidate_limit=1,
                    )
        self.assertEqual([row["guid"] for row in result.candidates], ["fresh"])

    def test_guid_upsert_updates_without_duplication(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            with db.session(path) as conn:
                fetch.upsert_episodes(conn, [episode("same", title="Old")])
                fetch.upsert_episodes(conn, [episode("same", title="Edited")])
                rows = conn.execute(
                    "SELECT guid, title FROM episode WHERE guid = 'same'"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [("same", "Edited")])

    def test_sent_for_one_subscriber_does_not_hide_shared_episode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.make_db(temp_dir)
            with db.session(path) as conn:
                episode_id = fetch.upsert_episodes(conn, [episode("shared")])[0]
                subscriber_id = conn.execute(
                    "INSERT INTO subscriber (email, unsub_token, confirm_token, status) "
                    "VALUES ('a@example.com', 'u', 'c', 'active')"
                ).lastrowid
                conn.execute(
                    "INSERT INTO sent (subscriber_id, episode_id, status) VALUES (?, ?, 'sent')",
                    (subscriber_id, episode_id),
                )
                run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                with patch.object(
                    fetch, "fetch_feeds", return_value=([episode("shared")], 1, 0)
                ):
                    result = fetch.fetch_all(
                        conn, since=0, run_id=run_id, refresh_discovery=False
                    )
            self.assertEqual([row["guid"] for row in result.candidates], ["shared"])

    def test_all_feed_failures_raise(self):
        shows = [
            {"feed_id": 1, "show_name": "One"},
            {"feed_id": 2, "show_name": "Two"},
        ]
        with patch.object(
            fetch.podcastindex,
            "episodes_by_feed_ids",
            side_effect=fetch.podcastindex.PodcastIndexError("down"),
        ):
            with self.assertRaises(fetch.FetchError):
                fetch.fetch_feeds(shows, since=0)


if __name__ == "__main__":
    unittest.main()
