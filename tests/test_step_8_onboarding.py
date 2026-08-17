"""Public signup, single opt-in, and unsubscribe safety gates."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from markupsafe import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-3-onboarding"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import app as onboarding  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "onboarding.db"
        db.init_db(self.path)
        self.database_patch = patch.object(config, "DATABASE_URL", f"file:{self.path}")
        self.database_patch.start()
        self.key_patch = patch.object(config, "RESEND_API_KEY", "test-resend-key")
        self.key_patch.start()
        onboarding._SIGNUPS.clear()
        onboarding.app.config.update(TESTING=True)
        self.client = onboarding.app.test_client()

    def tearDown(self):
        self.key_patch.stop()
        self.database_patch.stop()
        self.temp.cleanup()

    def signup(self, email="reader@example.com", topics=("technology-ai", "science")):
        fields = {"email": email, "topic": list(topics), "company": ""}
        return self.client.post("/subscribe", data=fields)

    def row(self, email="reader@example.com"):
        with db.session(self.path) as conn:
            subscriber = conn.execute(
                "SELECT * FROM subscriber WHERE email=?", (email,)
            ).fetchone()
            if subscriber is None:
                return None, []
            topics = conn.execute(
                "SELECT topic FROM subscription WHERE subscriber_id=? ORDER BY topic",
                (subscriber["id"],),
            ).fetchall()
            return dict(subscriber), [row["topic"] for row in topics]

    def test_render_has_exact_topic_controls_and_no_free_text_or_polling(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('name="topic"'), len(config.TOPICS))
        self.assertIn('type="email"', html)
        self.assertNotIn("<textarea", html)
        self.assertNotIn("/status/", html)
        self.assertNotIn("job_id", html)

    def test_valid_signup_is_active_stores_topics_and_sends_welcome(self):
        with patch.object(onboarding, "send_welcome", return_value="message-1") as send:
            response = self.signup(topics=("science", "travel", "design"))
        subscriber, topics = self.row()
        self.assertEqual(response.status_code, 200)
        # Jinja escapes the apostrophe, so compare against the escaped constant
        # rather than a literal that silently stops matching.
        self.assertIn(str(escape(onboarding.SUBSCRIBED_TITLE)), response.get_data(as_text=True))
        self.assertEqual(subscriber["status"], "active")
        self.assertIsNotNone(subscriber["confirmed_at"])
        self.assertEqual(topics, ["design", "science", "travel"])
        self.assertNotEqual(subscriber["confirm_token"], subscriber["unsub_token"])
        self.assertGreaterEqual(len(subscriber["confirm_token"]), 40)
        self.assertGreaterEqual(len(subscriber["unsub_token"]), 40)
        # _selected_topics() re-orders submitted topics into config.TOPICS order.
        submitted = {"science", "travel", "design"}
        expected_order = [slug for slug in config.TOPIC_SLUGS if slug in submitted]
        send.assert_called_once_with(
            "reader@example.com", expected_order, subscriber["unsub_token"]
        )

    def test_invalid_email_empty_topics_and_unknown_topic_write_nothing(self):
        with patch.object(onboarding, "send_welcome"):
            bad_email = self.signup(email="not-an-email")
            no_topics = self.signup(email="empty@example.com", topics=())
            unknown = self.signup(email="unknown@example.com", topics=("made-up",))
        self.assertEqual((bad_email.status_code, no_topics.status_code, unknown.status_code), (400, 400, 400))
        with db.session(self.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0]
        self.assertEqual(count, 0)

    def test_duplicate_updates_topics_without_second_subscriber(self):
        with patch.object(onboarding, "send_welcome") as send:
            self.signup(topics=("technology-ai", "science"))
            response = self.signup(topics=("travel",))
        updated, topics = self.row()
        with db.session(self.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0]
        self.assertEqual(response.status_code, 200)
        self.assertIn("Preferences updated", response.get_data(as_text=True))
        self.assertEqual((count, updated["status"], topics), (1, "active", ["travel"]))
        self.assertEqual(send.call_count, 1)

    def test_confirm_link_is_a_harmless_noop(self):
        with patch.object(onboarding, "send_welcome"):
            self.signup()
        subscriber, _ = self.row()
        original_status = subscriber["status"]
        original_confirmed_at = subscriber["confirmed_at"]
        first = self.client.get(f"/confirm/{subscriber['confirm_token']}")
        second = self.client.get(f"/confirm/{subscriber['confirm_token']}")
        after, _ = self.row()
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(after["status"], original_status)
        self.assertEqual(after["confirmed_at"], original_confirmed_at)

    def test_unsubscribe_get_is_read_only_and_posts_are_idempotent(self):
        with patch.object(onboarding, "send_welcome"):
            self.signup()
        subscriber, _ = self.row()
        get_response = self.client.get(f"/unsubscribe/{subscriber['unsub_token']}")
        still_active, _ = self.row()
        not_me_response = self.client.get(f"/unsubscribe/{subscriber['unsub_token']}?not-me=1")
        still_active_after_not_me, _ = self.row()
        first = self.client.post(f"/unsubscribe/{subscriber['unsub_token']}")
        second = self.client.post(f"/unsubscribe/{subscriber['unsub_token']}")
        unknown = self.client.post("/unsubscribe/not-a-real-token")
        unsubscribed, _ = self.row()
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(still_active["status"], "active")
        self.assertEqual(not_me_response.status_code, 200)
        self.assertEqual(still_active_after_not_me["status"], "active")
        self.assertEqual(unsubscribed["status"], "unsubscribed")
        self.assertEqual((first.status_code, second.status_code, unknown.status_code), (200, 200, 200))
        self.assertEqual(first.get_data(as_text=True), unknown.get_data(as_text=True))

    def test_honeypot_is_silent_and_creates_nothing(self):
        with patch.object(onboarding, "send_welcome") as send:
            response = self.client.post(
                "/subscribe",
                data={
                    "email": "bot@example.com",
                    "topic": "science",
                    "company": "Spam Incorporated",
                },
            )
            genuine = self.signup()
        subscriber, _ = self.row("bot@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(subscriber)
        send.assert_called_once()  # the genuine signup only
        # The trap is worthless if its response differs from a real signup's.
        self.assertEqual(response.get_data(), genuine.get_data())

    def test_rate_limit_blocks_sixth_attempt(self):
        with patch.object(onboarding, "send_welcome"):
            responses = [
                self.signup(email=f"reader-{index}@example.com", topics=("science",))
                for index in range(config.SIGNUP_RATE_LIMIT + 1)
            ]
        self.assertTrue(all(response.status_code == 200 for response in responses[:-1]))
        self.assertEqual(responses[-1].status_code, 429)

    def test_resubscribe_after_unsubscribe_rotates_tokens_and_reactivates(self):
        with patch.object(onboarding, "send_welcome") as send:
            self.signup()
            original, _ = self.row()
            self.client.post(f"/unsubscribe/{original['unsub_token']}")
            self.signup(topics=("travel",))
        renewed, topics = self.row()
        self.assertEqual((renewed["status"], topics), ("active", ["travel"]))
        self.assertNotEqual(renewed["confirm_token"], original["confirm_token"])
        self.assertNotEqual(renewed["unsub_token"], original["unsub_token"])
        self.assertEqual(send.call_count, 2)

    def test_welcome_email_carries_unsubscribe_topics_and_no_images(self):
        with onboarding.app.test_request_context():
            with patch.object(
                onboarding.resend.Emails, "send", return_value={"id": "welcome-1"}
            ) as send:
                onboarding.send_welcome(
                    "reader@example.com", ["science", "travel"], "unsub-token"
                )
        payload = send.call_args[0][0]
        html = payload["html"]
        text = payload["text"]
        self.assertIn("/unsubscribe/unsub-token", html)
        self.assertIn("?not-me=1", html)
        labels = dict(config.TOPICS)
        self.assertIn(labels["science"], html)
        self.assertIn(labels["travel"], html)
        self.assertNotIn("<img", html)
        self.assertTrue(text)
        self.assertIn("/unsubscribe/unsub-token", text)
        self.assertEqual(
            payload["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click"
        )

    def test_welcome_send_failure_leaves_subscriber_active(self):
        with patch.object(
            onboarding, "send_welcome", side_effect=onboarding.WelcomeEmailError("boom")
        ):
            response = self.signup()
        subscriber, _ = self.row()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(subscriber["status"], "active")


if __name__ == "__main__":
    unittest.main()
