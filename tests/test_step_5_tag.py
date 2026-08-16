"""Deterministic gates for shared episode tagging."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-5-tag"))
sys.path.insert(0, str(ROOT / "block-4-fetch"))
sys.path.insert(0, str(ROOT / "block-2-universe"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import tag  # noqa: E402


class FakeGroq:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def create(self, **kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        message = SimpleNamespace(content=value)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=100),
        )


def payload(entries) -> str:
    return json.dumps({"episodes": entries})


def valid_entry(index: int, topics=None, score: int = 80):
    return {
        "id": index,
        "topics": ["technology-ai"] if topics is None else topics,
        "score": score,
        "why": "Ada Lovelace explains how the analytical engine changes computing.",
    }


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tag.db"
        db.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def add_episodes(self, count: int) -> list[int]:
        with db.session(self.path) as conn:
            ids = []
            for index in range(count):
                ids.append(
                    conn.execute(
                        "INSERT INTO episode "
                        "(guid, feed_id, show_name, title, description, duration_sec, published_at) "
                        "VALUES (?, ?, 'Show', ?, ?, 1800, datetime('now'))",
                        (
                            f"guid-{index}",
                            index + 1,
                            f"Episode {index}",
                            "Ada Lovelace discusses the analytical engine and modern computing " * 3,
                        ),
                    ).lastrowid
                )
        return ids

    def run_tag(self, client, **kwargs):
        with (
            patch.object(tag, "log_call"),
            patch.object(tag.time, "sleep"),
            db.session(self.path) as conn,
        ):
            return tag.tag_all(conn, client=client, **kwargs)

    def test_valid_response_writes_score_topics_and_attempt(self):
        episode_id = self.add_episodes(1)[0]
        client = FakeGroq(
            [payload([valid_entry(1, ["technology-ai", "unknown", "science"])])]
        )
        result = self.run_tag(client)
        self.assertEqual((result.tagged, result.tokens_used), (1, 100))
        with db.session(self.path) as conn:
            row = conn.execute(
                "SELECT score, tagged_at, tag_attempts, tag_error FROM episode WHERE id=?",
                (episode_id,),
            ).fetchone()
            topics = conn.execute(
                "SELECT topic FROM episode_topic WHERE episode_id=? ORDER BY topic",
                (episode_id,),
            ).fetchall()
        self.assertEqual((row["score"], row["tag_attempts"], row["tag_error"]), (80, 1, None))
        self.assertIsNotNone(row["tagged_at"])
        self.assertEqual([item[0] for item in topics], ["science", "technology-ai"])

    def test_unknown_topics_drop_topic_cap_and_empty_topics_are_valid(self):
        parsed, invalid = tag.parse_tags(
            payload(
                [
                    valid_entry(
                        1,
                        [
                            "technology-ai", "science", "history", "finance", "unknown",
                        ],
                    ),
                    valid_entry(2, []),
                ]
            ),
            2,
        )
        self.assertEqual(invalid, 0)
        self.assertEqual(len(parsed[1].topics), config.TAG_MAX_TOPICS)
        self.assertEqual(parsed[2].topics, [])

    def test_generic_reason_is_retried_not_tagged(self):
        episode_id = self.add_episodes(1)[0]
        generic = valid_entry(1)
        generic["why"] = "A great listen for anyone interested in AI."
        result = self.run_tag(FakeGroq([payload([generic])]))
        self.assertEqual((result.tagged, result.generic), (0, 1))
        with db.session(self.path) as conn:
            row = conn.execute(
                "SELECT tagged_at, tag_attempts, tag_error FROM episode WHERE id=?",
                (episode_id,),
            ).fetchone()
        self.assertIsNone(row["tagged_at"])
        self.assertEqual(row["tag_attempts"], 1)
        self.assertIn("generic", row["tag_error"])

    def test_two_parse_failures_do_not_crash_and_third_attempt_can_succeed(self):
        episode_id = self.add_episodes(1)[0]
        failed = self.run_tag(FakeGroq(["not json", "still not json"]))
        self.assertEqual(failed.parse_failed, 1)
        with db.session(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT tag_attempts FROM episode WHERE id=?", (episode_id,)
                ).fetchone()[0],
                2,
            )

        succeeded = self.run_tag(FakeGroq([payload([valid_entry(1)])]))
        self.assertEqual(succeeded.tagged, 1)
        with db.session(self.path) as conn:
            attempts = conn.execute(
                "SELECT tag_attempts FROM episode WHERE id=?", (episode_id,)
            ).fetchone()[0]
        self.assertEqual(attempts, 3)

    def test_daily_budget_leaves_unattempted_rows_untouched(self):
        episode_id = self.add_episodes(1)[0]
        client = FakeGroq([payload([valid_entry(1)])])
        result = self.run_tag(client, daily_budget=1)
        self.assertTrue(result.budget_exhausted)
        self.assertEqual(client.calls, 0)
        with db.session(self.path) as conn:
            attempts = conn.execute(
                "SELECT tag_attempts FROM episode WHERE id=?", (episode_id,)
            ).fetchone()[0]
        self.assertEqual(attempts, 0)

    def test_completed_batches_survive_later_api_failure(self):
        ids = self.add_episodes(config.TAG_BATCH_SIZE + 1)
        first = payload([valid_entry(index) for index in range(1, config.TAG_BATCH_SIZE + 1)])
        client = FakeGroq([first, RuntimeError("provider down")])
        with self.assertRaises(tag.TagError):
            self.run_tag(client)
        with db.session(self.path) as conn:
            tagged = conn.execute(
                "SELECT COUNT(*) FROM episode WHERE tagged_at IS NOT NULL"
            ).fetchone()[0]
            final = conn.execute(
                "SELECT tag_attempts, tag_error FROM episode WHERE id=?", (ids[-1],)
            ).fetchone()
        self.assertEqual(tagged, config.TAG_BATCH_SIZE)
        self.assertEqual(final["tag_attempts"], 1)
        self.assertIn("provider down", final["tag_error"])


if __name__ == "__main__":
    unittest.main()
