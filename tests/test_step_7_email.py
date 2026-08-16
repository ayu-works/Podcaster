"""Delivery selection, rendering, and irreversible-send safety gates."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-6-email"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import email_out  # noqa: E402


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "email.db"
        db.init_db(self.path)
        with db.session(self.path) as conn:
            self.run_id = conn.execute(
                "INSERT INTO run (fetch_cutoff_at) VALUES ('2026-08-16 10:00:00')"
            ).lastrowid

    def tearDown(self):
        self.temp.cleanup()

    def add_subscriber(self, conn, suffix: str, topics=("technology-ai",), status="active"):
        subscriber_id = conn.execute(
            "INSERT INTO subscriber "
            "(email, unsub_token, confirm_token, status, confirmed_at) "
            "VALUES (?, ?, ?, ?, CASE WHEN ?='active' THEN datetime('now') END)",
            (f"{suffix}@example.com", f"unsub-{suffix}", f"confirm-{suffix}", status, status),
        ).lastrowid
        conn.executemany(
            "INSERT INTO subscription (subscriber_id, topic) VALUES (?, ?)",
            [(subscriber_id, topic) for topic in topics],
        )
        return subscriber_id

    def add_pick(
        self,
        conn,
        guid: str,
        *,
        topic="technology-ai",
        feed_id=1,
        score=90,
        title=None,
        rank=1,
    ):
        episode_id = conn.execute(
            "INSERT INTO episode "
            "(guid, feed_id, show_name, title, description, duration_sec, "
            "published_at, web_url, score, why, tagged_at) "
            "VALUES (?, ?, 'Useful Show', ?, 'long description', 1800, "
            "'2026-08-16 09:00:00', 'https://example.com/listen', ?, "
            "'Explains the concrete mechanism behind this result.', datetime('now'))",
            (guid, feed_id, title or guid, score),
        ).lastrowid
        conn.execute(
            "INSERT INTO daily_pick (run_id, topic, episode_id, rank) VALUES (?, ?, ?, ?)",
            (self.run_id, topic, episode_id, rank),
        )
        return episode_id

    def test_topic_merge_dedupe_caps_and_rendering(self):
        with db.session(self.path) as conn:
            subscriber_id = self.add_subscriber(
                conn, "reader", ("technology-ai", "science", "travel")
            )
            shared = self.add_pick(
                conn, "shared", title='A <B> & "C"', topic="technology-ai", score=100
            )
            conn.execute(
                "INSERT INTO daily_pick (run_id, topic, episode_id, rank) VALUES (?, 'science', ?, 1)",
                (self.run_id, shared),
            )
            for index in range(4):
                self.add_pick(
                    conn,
                    f"same-show-{index}",
                    topic="science" if index % 2 else "travel",
                    feed_id=99,
                    score=99 - index,
                    rank=index + 2,
                )
            for index in range(20):
                self.add_pick(
                    conn,
                    f"extra-{index}",
                    topic="technology-ai",
                    feed_id=200 + index,
                    score=80 - index,
                    rank=index + 2,
                )

            picks = email_out.load_picks(conn, subscriber_id, self.run_id)
            html = email_out.render(picks, "unsub-reader")

        self.assertEqual(len(picks), config.MAX_PER_EMAIL)
        self.assertEqual(sum(pick["id"] == shared for pick in picks), 1)
        self.assertLessEqual(
            sum(pick["feed_id"] == 99 for pick in picks),
            config.MAX_PER_SHOW_PER_EMAIL,
        )
        self.assertIn("Technology &amp; AI", html)
        self.assertIn("Science", html)
        self.assertIn("Travel", html)
        self.assertIn("A &lt;B&gt; &amp; &#34;C&#34;", html)
        self.assertIn("30m", html)
        self.assertIn("https://example.com/listen", html)
        self.assertIn("/unsubscribe/unsub-reader", html)
        self.assertIn("max-width:600px", html)
        self.assertNotIn("<img", html)
        self.assertLess(len(html.encode()), 102_000)

    def test_sent_and_pending_excluded_failed_retried(self):
        with db.session(self.path) as conn:
            subscriber_id = self.add_subscriber(conn, "states")
            sent_id = self.add_pick(conn, "sent", rank=1)
            pending_id = self.add_pick(conn, "pending", rank=2)
            failed_id = self.add_pick(conn, "failed", rank=3)
            conn.executemany(
                "INSERT INTO sent (subscriber_id, episode_id, run_id, status, attempts) "
                "VALUES (?, ?, ?, ?, 1)",
                [
                    (subscriber_id, sent_id, self.run_id, "sent"),
                    (subscriber_id, pending_id, self.run_id, "pending"),
                    (subscriber_id, failed_id, self.run_id, "failed"),
                ],
            )
            picks = email_out.load_picks(conn, subscriber_id, self.run_id)
        self.assertEqual([pick["id"] for pick in picks], [failed_id])

    def test_pending_is_committed_before_external_send(self):
        with db.session(self.path) as conn:
            subscriber_id = self.add_subscriber(conn, "ordered")
            episode_id = self.add_pick(conn, "ordered-pick")
            subscriber = conn.execute(
                "SELECT id, email, unsub_token FROM subscriber WHERE id=?",
                (subscriber_id,),
            ).fetchone()

            def observe_pending(*_args):
                observer = db.connect(self.path)
                try:
                    row = observer.execute(
                        "SELECT status, attempts FROM sent "
                        "WHERE subscriber_id=? AND episode_id=?",
                        (subscriber_id, episode_id),
                    ).fetchone()
                    self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
                finally:
                    observer.close()
                return "message-1"

            with patch.object(
                email_out.db,
                "ensure_connection",
                wraps=email_out.db.ensure_connection,
            ) as refresh, patch.object(email_out, "send", side_effect=observe_pending):
                delivery = email_out.deliver_subscriber(conn, subscriber, self.run_id)
        self.assertEqual(delivery.kind, "sent")
        self.assertEqual(refresh.call_count, 1)

    def test_failure_isolated_and_failed_attempt_retries(self):
        with db.session(self.path) as conn:
            ids = [self.add_subscriber(conn, name) for name in ("one", "two", "three")]
            episode_id = self.add_pick(conn, "shared-pick")

            def fake_send(to, *_args):
                if to == "two@example.com":
                    raise email_out.EmailError("timeout on two")
                return "message-ok"

            with patch.object(email_out, "send", side_effect=fake_send):
                result = email_out.deliver_all(conn, self.run_id)

            states = conn.execute(
                "SELECT subscriber_id, status, attempts, last_error FROM sent "
                "WHERE episode_id=? ORDER BY subscriber_id",
                (episode_id,),
            ).fetchall()
            with patch.object(
                email_out, "send", side_effect=email_out.EmailError("timeout again")
            ):
                retry = email_out.deliver_all(conn, self.run_id)
            retried = conn.execute(
                "SELECT status, attempts, last_error FROM sent "
                "WHERE subscriber_id=? AND episode_id=?",
                (ids[1], episode_id),
            ).fetchone()

        self.assertEqual((result.sent, result.failed), (2, 1))
        self.assertEqual([row["status"] for row in states], ["sent", "failed", "sent"])
        self.assertIn("timeout on two", states[1]["last_error"])
        self.assertEqual((retry.sent, retry.failed, retry.skipped), (0, 1, 2))
        self.assertEqual((retried["status"], retried["attempts"]), ("failed", 2))
        self.assertIn("timeout again", retried["last_error"])

    def test_inactive_and_empty_subscribers_are_skipped(self):
        with db.session(self.path) as conn:
            self.add_subscriber(conn, "active-empty", ("travel",))
            self.add_subscriber(conn, "paused", status="paused")
            self.add_subscriber(conn, "unsubscribed", status="unsubscribed")
            self.add_subscriber(conn, "pending", status="pending")
            self.add_pick(conn, "tech-only")
            with patch.object(email_out, "send") as mocked_send:
                result = email_out.deliver_all(conn, self.run_id)
            sent_count = conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
        self.assertEqual((result.sent, result.failed, result.skipped), (0, 0, 1))
        self.assertEqual(sent_count, 0)
        mocked_send.assert_not_called()

    def test_targeted_delivery_never_sends_to_other_active_subscribers(self):
        with db.session(self.path) as conn:
            self.add_subscriber(conn, "target")
            self.add_subscriber(conn, "other")
            self.add_pick(conn, "targeted-pick")
            with patch.object(email_out, "send", return_value="message-1") as send:
                result = email_out.deliver_all(
                    conn,
                    self.run_id,
                    email="  TARGET@example.com ",
                )

        self.assertEqual((result.sent, result.failed), (1, 0))
        self.assertEqual(send.call_args.args[0], "target@example.com")
        self.assertEqual(send.call_count, 1)

    def test_short_delivery_caps_merged_email_at_two_picks(self):
        with db.session(self.path) as conn:
            subscriber_id = self.add_subscriber(conn, "short")
            for index in range(5):
                self.add_pick(conn, f"short-{index}", rank=index + 1)
            picks = email_out.load_picks(
                conn,
                subscriber_id,
                self.run_id,
                max_picks=2,
            )
        self.assertEqual(len(picks), 2)

    def test_short_delivery_sends_nothing_until_two_unique_picks_exist(self):
        with db.session(self.path) as conn:
            self.add_subscriber(conn, "not-enough")
            self.add_pick(conn, "only-one")
            with patch.object(email_out, "send") as send:
                result = email_out.deliver_all(
                    conn,
                    self.run_id,
                    email="not-enough@example.com",
                    max_picks=2,
                    min_picks=2,
                )
        self.assertEqual((result.sent, result.skipped), (0, 1))
        send.assert_not_called()

    def test_resend_headers(self):
        with patch.object(config, "RESEND_API_KEY", "valid-key"), patch.object(
            email_out.resend.Emails, "send", return_value={"id": "resend-1"}
        ) as resend_send:
            message_id = email_out.send(
                "reader@example.com", "Subject", "<p>Hello</p>", "token-1"
            )
        payload = resend_send.call_args.args[0]
        self.assertEqual(message_id, "resend-1")
        self.assertIn("List-Unsubscribe", payload["headers"])
        self.assertEqual(
            payload["headers"]["List-Unsubscribe-Post"],
            "List-Unsubscribe=One-Click",
        )


if __name__ == "__main__":
    unittest.main()
