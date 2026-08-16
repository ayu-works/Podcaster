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


class FakeClock:
    """Monotonic time the test owns, so deadline gates need no real waiting.

    `tick` charges every read, which is how a test can put the deadline in the
    past before the first batch is even considered.
    """

    def __init__(self, start: float = 1_000.0, tick: float = 0.0):
        self.now = start
        self.tick = tick

    def monotonic(self) -> float:
        current = self.now
        self.now += self.tick
        return current


class ClockedGroq(FakeGroq):
    """FakeGroq that charges each call a fixed amount of fake wall clock."""

    def __init__(self, responses, clock: FakeClock, seconds: float):
        super().__init__(responses)
        self.clock = clock
        self.seconds = seconds

    def create(self, **kwargs):
        self.clock.now += self.seconds
        return super().create(**kwargs)


class VaryingClockedGroq(ClockedGroq):
    """ClockedGroq whose successive calls cost different amounts of clock.

    Lets a test tell apart the two possible deadline estimates: the slowest
    batch seen so far, or merely the most recent one.
    """

    def __init__(self, responses, clock: FakeClock, schedule: list[float]):
        super().__init__(responses, clock, 0.0)
        self.schedule = list(schedule)

    def create(self, **kwargs):
        self.seconds = self.schedule[min(self.calls, len(self.schedule) - 1)]
        return super().create(**kwargs)


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

    def run_tag_clocked(self, responses, clock, seconds, **kwargs):
        """Run tagging on a fake clock and hand back the result and the double."""
        client = ClockedGroq(responses, clock, seconds)
        with patch.object(tag.time, "monotonic", clock.monotonic):
            return self.run_tag(client, **kwargs), client

    def full_batches(self, count: int) -> list[str]:
        return [
            payload(
                [valid_entry(index) for index in range(1, config.TAG_BATCH_SIZE + 1)]
            )
            for _ in range(count)
        ]

    def episode_state(self, episode_id: int):
        with db.session(self.path) as conn:
            return conn.execute(
                "SELECT tagged_at, tag_attempts, tag_error FROM episode WHERE id=?",
                (episode_id,),
            ).fetchone()

    def tagged_count(self) -> int:
        with db.session(self.path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM episode WHERE tagged_at IS NOT NULL"
            ).fetchone()[0]

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

    def test_attempt_is_committed_before_waiting_for_groq(self):
        episode_id = self.add_episodes(1)[0]
        observed_attempts = []

        class ObservingGroq(FakeGroq):
            def create(inner_self, **kwargs):
                with db.session(self.path) as observer:
                    observed_attempts.append(
                        observer.execute(
                            "SELECT tag_attempts FROM episode WHERE id=?",
                            (episode_id,),
                        ).fetchone()[0]
                    )
                return super().create(**kwargs)

        self.run_tag(ObservingGroq([payload([valid_entry(1)])]))
        self.assertEqual(observed_attempts, [1])

    def test_ten_episode_batch_uses_set_based_writes_and_reports_groq_request(self):
        self.add_episodes(10)
        client = FakeGroq(
            [payload([valid_entry(index) for index in range(1, 11)])]
        )
        requests = []

        class NoExecuteMany:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                if name == "executemany":
                    raise AssertionError("tagging must not issue one remote write per row")
                return getattr(self.connection, name)

        with (
            patch.object(tag, "log_call"),
            patch.object(tag.time, "sleep"),
            db.session(self.path) as conn,
        ):
            result = tag.tag_all(
                NoExecuteMany(conn),
                client=client,
                episode_ids=list(range(1, 11)),
                request_progress=lambda batch, attempt: requests.append(
                    (batch, attempt)
                ),
            )

        self.assertEqual(result.tagged, 10)
        self.assertEqual(requests, [(1, 1)])

    def test_expired_deadline_still_runs_one_batch(self):
        ids = self.add_episodes(config.TAG_BATCH_SIZE + 1)
        # The clock is already past the deadline when the first batch is
        # considered, and one batch must still run.
        clock = FakeClock(tick=1.0)
        result, client = self.run_tag_clocked(
            self.full_batches(1) + [payload([valid_entry(1)])],
            clock,
            seconds=1.0,
            deadline_seconds=0.5,
        )
        self.assertTrue(result.deadline_reached)
        self.assertEqual(client.calls, 1)
        self.assertEqual((result.tagged, self.tagged_count()), (20, 20))
        self.assertIsNone(self.episode_state(ids[-1])["tagged_at"])

    def test_deadline_keeps_finished_batches_and_requeues_the_rest(self):
        ids = self.add_episodes(2 * config.TAG_BATCH_SIZE + 1)
        clock = FakeClock()
        result, client = self.run_tag_clocked(
            self.full_batches(2) + [payload([valid_entry(1)])],
            clock,
            seconds=1.0,
            deadline_seconds=2.5,
        )
        self.assertTrue(result.deadline_reached)
        self.assertFalse(result.budget_exhausted)
        self.assertEqual(client.calls, 2)

        # Two batches committed and stay committed; the third is untouched, so
        # it is still queue work rather than a failure the next run must retry.
        self.assertEqual((result.tagged, self.tagged_count()), (40, 40))
        skipped = self.episode_state(ids[-1])
        self.assertIsNone(skipped["tagged_at"])
        self.assertEqual((skipped["tag_attempts"], skipped["tag_error"]), (0, None))
        self.assertEqual(result.untagged_left, 1)

    def test_estimate_follows_the_slowest_batch_not_the_most_recent(self):
        """One quick batch must not admit a slow batch that overruns the step.

        Batches cost 8s then 1s against a 16s deadline. Estimating from the
        slowest batch stops after two calls; estimating from the most recent
        one would see a 1s batch, admit a third call, and run past the wall
        that killed the stage in production.
        """
        self.add_episodes(3 * config.TAG_BATCH_SIZE)
        clock = FakeClock()
        client = VaryingClockedGroq(self.full_batches(3), clock, [8.0, 1.0, 8.0])
        with patch.object(tag.time, "monotonic", clock.monotonic):
            result = self.run_tag(client, deadline_seconds=16)

        self.assertTrue(result.deadline_reached)
        self.assertEqual(client.calls, 2)
        self.assertEqual((result.tagged, self.tagged_count()), (40, 40))
        self.assertEqual(result.untagged_left, config.TAG_BATCH_SIZE)

    def test_pacing_sleep_stops_rather_than_crossing_the_deadline(self):
        ids = self.add_episodes(config.TAG_BATCH_SIZE + 1)
        clock = FakeClock()
        # One token-per-second tier makes the pacing interval, not the batch
        # estimate, the thing that would overrun the deadline.
        with patch.object(config, "GROQ_TPM", 60):
            result, client = self.run_tag_clocked(
                self.full_batches(1) + [payload([valid_entry(1)])],
                clock,
                seconds=1.0,
                deadline_seconds=50,
            )
        self.assertTrue(result.deadline_reached)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.tagged_count(), 20)
        skipped = self.episode_state(ids[-1])
        self.assertEqual((skipped["tag_attempts"], skipped["tag_error"]), (0, None))

    def test_zero_deadline_disables_the_gate_entirely(self):
        self.add_episodes(2 * config.TAG_BATCH_SIZE + 1)
        clock = FakeClock()
        result, client = self.run_tag_clocked(
            self.full_batches(2) + [payload([valid_entry(1)])],
            clock,
            seconds=10_000.0,
            deadline_seconds=0,
        )
        self.assertFalse(result.deadline_reached)
        self.assertEqual(client.calls, 3)
        self.assertEqual((result.tagged, self.tagged_count()), (41, 41))

    def test_budget_and_deadline_are_independent_outcomes(self):
        self.add_episodes(config.TAG_BATCH_SIZE + 1)
        clock = FakeClock()
        result, client = self.run_tag_clocked(
            self.full_batches(1) + [payload([valid_entry(1)])],
            clock,
            seconds=1.0,
            deadline_seconds=0,
            daily_budget=1,
        )
        self.assertTrue(result.budget_exhausted)
        self.assertFalse(result.deadline_reached)
        self.assertEqual((client.calls, self.tagged_count()), (0, 0))


if __name__ == "__main__":
    unittest.main()
