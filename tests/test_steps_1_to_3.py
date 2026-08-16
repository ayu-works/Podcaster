"""Deterministic gates for implementation-plan Steps 1 through 3."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-2-universe"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import universe  # noqa: E402


class FakeGroq:
    def __init__(self, terms_per_interest: int | None = None):
        self.terms_per_interest = terms_per_interest or config.TERMS_PER_INTEREST
        self.batch_sizes: list[int] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        prompt = kwargs["messages"][1]["content"]
        inputs = [line for line in prompt.splitlines() if line.startswith("[")]
        self.batch_sizes.append(len(inputs))
        payload = {
            "interests": [
                {
                    "index": index,
                    "terms": [
                        f"topic{len(self.batch_sizes)}-{index}-term{term}"
                        for term in range(self.terms_per_interest)
                    ],
                }
                for index in range(len(inputs))
            ]
        }
        message = SimpleNamespace(content=json.dumps(payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def feed(
    feed_id: int,
    *,
    interests: set[int] | None = None,
    title: str | None = None,
    score: float = 1.0,
) -> universe.FeedHit:
    return universe.FeedHit(
        feed_id=feed_id,
        feed_url=f"https://example.com/{feed_id}.xml",
        title=title or f"Show {feed_id}",
        newest_item_pubdate=None,
        matched_interests=interests or set(),
        score=score,
    )


class ConfigTests(unittest.TestCase):
    def test_topics_are_the_single_valid_slug_set(self):
        self.assertEqual(len(config.TOPICS), 20)
        self.assertEqual(config.TOPIC_SLUGS, tuple(slug for slug, _ in config.TOPICS))
        self.assertEqual(config.TOPIC_LABELS, tuple(label for _, label in config.TOPICS))
        self.assertTrue(all(slug == slug.lower() and " " not in slug for slug in config.TOPIC_SLUGS))
        self.assertEqual(len(set(config.TOPIC_SLUGS)), 20)


class SchemaTests(unittest.TestCase):
    def test_stale_remote_stream_is_refreshed_with_one_safe_read_retry(self):
        class Cursor:
            @staticmethod
            def fetchone():
                return (1,)

        class StaleOnceConnection:
            def __init__(self):
                self.calls = 0

            def execute(self, sql):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("HTTP status 404: stream not found")
                return Cursor()

        conn = StaleOnceConnection()
        db.ensure_connection(conn)
        self.assertEqual(conn.calls, 2)

    def test_connection_refresh_does_not_retry_unrelated_errors(self):
        class BrokenConnection:
            calls = 0

            def execute(self, sql):
                self.calls += 1
                raise RuntimeError("permission denied")

        conn = BrokenConnection()
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            db.ensure_connection(conn)
        self.assertEqual(conn.calls, 1)

    def test_session_rollback_failure_preserves_body_exception(self):
        class RollbackFailingConnection:
            def commit(self):
                pass

            def rollback(self):
                raise RuntimeError("cleanup stream expired")

            def close(self):
                pass

        with patch.object(db, "connect", return_value=RollbackFailingConnection()):
            with self.assertRaisesRegex(ValueError, "original body failure"):
                with db.session():
                    raise ValueError("original body failure")

    def test_bulk_values_groups_remote_round_trips_and_keeps_parameters_bound(self):
        calls = []

        class RecordingConnection:
            def execute(self, sql, parameters):
                calls.append((sql, parameters))

        rows = [(index, f"row-{index}") for index in range(160)]
        db.execute_values(
            RecordingConnection(),
            "INSERT INTO sample (id, label) VALUES {values}",
            rows,
        )

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(len(parameters) <= 150 for _, parameters in calls))
        self.assertTrue(all("{values}" not in sql for sql, _ in calls))
        self.assertEqual(
            [value for _, parameters in calls for value in parameters],
            [value for row in rows for value in row],
        )

    def test_remote_turso_driver_provides_named_and_positional_rows(self):
        calls = []

        class FakeRow:
            def __init__(self, cursor, values):
                self._values = tuple(values)
                self._indexes = {
                    column[0]: index
                    for index, column in enumerate(cursor.description)
                }

            def __getitem__(self, key):
                if isinstance(key, str):
                    key = self._indexes[key]
                return self._values[key]

        class FakeCursor:
            description = (("one", None, None, None, None, None, None),)

            def __init__(self, connection, values=()):
                self.connection = connection
                self.values = values

            def fetchone(self):
                return self.connection.row_factory(self, self.values)

        class FakeConnection:
            row_factory = None

            def execute(self, sql):
                if sql == "SELECT 1 AS one":
                    return FakeCursor(self, (1,))
                return FakeCursor(self)

        def fake_connect(url, *, auth_token):
            calls.append((url, auth_token))
            return FakeConnection()

        fake_driver = SimpleNamespace(connect=fake_connect, Row=FakeRow)
        with patch.dict(sys.modules, {"turso_serverless": fake_driver}):
            conn = db.connect("  libsql://podcaster.example\n", token="token\n")

        self.assertEqual(calls, [("libsql://podcaster.example", "token")])
        row = conn.execute("SELECT 1 AS one").fetchone()
        self.assertEqual((row[0], row["one"]), (1, 1))

    def test_schema_is_idempotent_and_clock_uses_only_good_cutoffs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "schema.db"
            db.init_db(path)
            db.init_db(path)
            self.assertCountEqual(db.table_names(path), db.TABLES)

            with db.session(path) as conn:
                self.assertIsNone(db.last_good_cutoff(conn))
                conn.execute(
                    "INSERT INTO run (fetch_cutoff_at, finished_at, status) VALUES (?, ?, ?)",
                    ("2026-01-01 01:00:00", "2026-01-01 01:30:00", "failed"),
                )
                self.assertIsNone(db.last_good_cutoff(conn))
                conn.execute(
                    "INSERT INTO run (fetch_cutoff_at, finished_at, status) VALUES (?, ?, ?)",
                    ("2026-01-02 01:00:00", "2026-01-02 01:30:00", "ok"),
                )
                conn.execute(
                    "INSERT INTO run (fetch_cutoff_at, finished_at, status) VALUES (?, ?, ?)",
                    ("2026-01-03 01:00:00", "2026-01-03 01:30:00", "partial"),
                )
                self.assertEqual(db.last_good_cutoff(conn), "2026-01-03 01:00:00")

    def test_guid_is_unique(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "guid.db"
            db.init_db(path)
            with db.session(path) as conn:
                values = ("guid-1", 1, "Show", "Episode")
                conn.execute(
                    "INSERT INTO episode (guid, feed_id, show_name, title) VALUES (?, ?, ?, ?)",
                    values,
                )
                with self.assertRaises(Exception):
                    conn.execute(
                        "INSERT INTO episode (guid, feed_id, show_name, title) VALUES (?, ?, ?, ?)",
                        values,
                    )


class UniverseTests(unittest.TestCase):
    def test_all_topics_expand_in_bounded_batches(self):
        client = FakeGroq()
        with patch.object(universe, "EXPANSION_BATCH_PAUSE_SECONDS", 0):
            expanded = universe.expand_all_topics(list(config.TOPIC_LABELS), client=client)
        self.assertEqual(client.batch_sizes, [3, 3, 3, 3, 3, 3, 2])
        self.assertEqual([item.text for item in expanded], list(config.TOPIC_LABELS))
        self.assertTrue(all(len(item.terms) == config.TERMS_PER_INTEREST for item in expanded))

    def test_expansion_rejects_a_short_term_list(self):
        client = FakeGroq(config.TERMS_PER_INTEREST - 1)
        with self.assertRaises(universe.UniverseError):
            universe.expand_interests(["Design"], client=client)

    def test_search_deduplicates_feed_and_records_all_topics(self):
        interests = [
            universe.Interest("One", ["shared", "one"]),
            universe.Interest("Two", ["shared", "two"]),
        ]

        def search(term, client=None):
            return [
                {
                    "id": 7,
                    "url": "https://example.com/feed.xml",
                    "title": "Shared Show",
                    "newestItemPubdate": None,
                    "episodeCount": 10,
                    "language": "en",
                }
            ]

        with patch.object(universe.podcastindex, "search_shows", side_effect=search):
            hits = universe.search_all(interests)
        self.assertEqual(list(hits), [7])
        self.assertEqual(hits[7].matched_interests, {0, 1})
        self.assertEqual(hits[7].matched_terms, {"shared", "one", "two"})

    def test_round_robin_prevents_one_topic_from_monopolising(self):
        ranked = [feed(index, interests={0}, score=100 - index) for index in range(1, 21)]
        ranked.extend(feed(index, interests={1}, score=1) for index in range(101, 104))
        kept = universe.allocate(ranked, interest_count=2, target=6)
        self.assertEqual(sum(1 in item.matched_interests for item in kept), 3)
        self.assertEqual(sum(0 in item.matched_interests for item in kept), 3)

    def test_save_is_global_preserves_mute_and_rebuilds_topics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shows.db"
            db.init_db(path)
            first = [feed(1, interests={0, 1}), feed(2, interests={2})]
            with db.session(path) as conn:
                self.assertEqual(universe.save_shows(conn, first), 2)
                conn.execute("UPDATE show SET status = 'muted' WHERE feed_id = 1")

            second = [feed(1, interests={3}, title="Renamed"), feed(3, interests={4})]
            with db.session(path) as conn:
                universe.save_shows(conn, second)

            with db.session(path) as conn:
                rows = conn.execute(
                    "SELECT feed_id, title, status FROM show ORDER BY feed_id"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [(1, "Renamed", "muted"), (3, "Show 3", "active")],
                )
                topics = conn.execute(
                    "SELECT s.feed_id, st.topic FROM show_topic st "
                    "JOIN show s ON s.id = st.show_id ORDER BY s.feed_id, st.topic"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in topics],
                    [(1, config.TOPIC_SLUGS[3]), (3, config.TOPIC_SLUGS[4])],
                )

            with self.assertRaises(universe.UniverseError):
                with db.session(path) as conn:
                    universe.save_shows(conn, [])
            with db.session(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM show").fetchone()[0], 2)

    def test_review_sample_spreads_across_five_topics(self):
        kept = [
            feed(topic * 100 + index, interests={topic})
            for topic in range(len(config.TOPICS))
            for index in range(10)
        ]
        sample = universe.review_sample(kept)
        self.assertEqual(len(sample), 50)
        self.assertEqual(len({topic for topic, _ in sample}), 5)
        self.assertEqual(len({item.feed_id for _, item in sample}), 50)


if __name__ == "__main__":
    unittest.main()
