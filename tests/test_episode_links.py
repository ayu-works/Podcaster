"""Every link a subscriber can click must open a readable page, not audio.

The pipeline stored `enclosureUrl` whenever a feed omitted `link`, and a live
sample showed that was the majority of episodes, so these gates cover both the
ingest ladder and the independent render-time guard.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-6-email"))
sys.path.insert(0, str(ROOT / "block-4-fetch"))
sys.path.insert(0, str(ROOT / "block-2-universe"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import db  # noqa: E402
import email_out  # noqa: E402
import fetch  # noqa: E402
import links  # noqa: E402

EPISODE_PAGE = "https://showsite.example/episodes/267-chocolove"
AUDIO = "https://op3.dev/e/mp3s.example.com/PC20-267-Final.mp3"
AUDIO_TRACKED = "https://chrt.fm/track/ABC/traffic.example.com/ep.m4a?updated=1712"

# Anything a mail client would open as a player or a download.
MEDIA_SAMPLES = (
    AUDIO,
    AUDIO_TRACKED,
    "https://cdn.example.com/show/episode.mp3",
    "https://cdn.example.com/show/episode.M4A",
    "https://cdn.example.com/show/episode.ogg",
    "https://cdn.example.com/show/episode.opus",
    "https://cdn.example.com/show/episode.wav",
    "https://cdn.example.com/show/video.mp4",
)


def item(**overrides) -> dict:
    """A Podcast Index episode item, shaped like the live API response."""
    base = {
        "guid": "guid-1",
        "feedId": 1,
        "feedTitle": "Useful Show",
        "title": "Episode 267",
        "description": "x" * 200,
        "duration": 1800,
        "datePublished": 1_760_000_000,
        "episodeType": "full",
        "link": EPISODE_PAGE,
        "enclosureUrl": AUDIO,
        "enclosureType": "audio/mpeg",
        "feedItunesId": 1150510297,
    }
    base.update(overrides)
    return base


class LinkChoiceTests(unittest.TestCase):
    def test_canonical_episode_page_is_preferred(self):
        self.assertEqual(fetch.to_episode(item(), "Show").web_url, EPISODE_PAGE)

    def test_audio_enclosure_is_never_stored_as_web_url(self):
        # The exact regression: no `link`, so the old code used the enclosure.
        for missing in (None, "", "   "):
            episode = fetch.to_episode(item(link=missing), "Show")
            self.assertNotEqual(episode.web_url, AUDIO)
            self.assertFalse(links.is_media_url(episode.web_url))

        # A feed that copies the audio URL into <link> is caught too, even
        # when the audio path carries no file extension.
        extensionless = "https://traffic.example.com/ABC123456"
        episode = fetch.to_episode(
            item(link=extensionless, enclosureUrl=extensionless), "Show"
        )
        self.assertNotEqual(episode.web_url, extensionless)

        for media in MEDIA_SAMPLES:
            episode = fetch.to_episode(item(link=media, enclosureUrl=media), "Show")
            self.assertFalse(
                links.is_media_url(episode.web_url),
                f"{media} survived as web_url",
            )

    def test_missing_episode_page_falls_back_to_a_show_page(self):
        episode = fetch.to_episode(item(link=None), "Show")
        self.assertEqual(
            episode.web_url, "https://podcasts.apple.com/podcast/id1150510297"
        )
        self.assertTrue(links.is_page_url(episode.web_url))

    def test_no_page_and_no_show_id_stores_nothing_rather_than_audio(self):
        episode = fetch.to_episode(item(link=None, feedItunesId=None), "Show")
        self.assertEqual(episode.web_url, "")

    def test_non_web_schemes_and_relative_links_are_rejected(self):
        for bad in ("javascript:alert(1)", "/relative/path", "ftp://x.example/a", "  "):
            self.assertEqual(links.safe_page_url(bad), "")
        self.assertFalse(links.is_page_url("mailto:someone@example.com"))

    def test_apple_show_url_rejects_non_numeric_ids(self):
        for bad in (None, "", "abc", 0, -5, "12x"):
            self.assertEqual(links.apple_show_url(bad), "")
        self.assertEqual(
            links.apple_show_url("42"), "https://podcasts.apple.com/podcast/id42"
        )


class RenderGuardTests(unittest.TestCase):
    """A poisoned row must not be able to produce an audio link in mail."""

    def pick(self, web_url: str) -> dict:
        return {
            "id": 1,
            "topic": "technology-ai",
            "feed_id": 1,
            "show_name": "Useful Show",
            "title": "Episode 267",
            "why": "Names the specific mechanism behind the result.",
            "duration_sec": 1800,
            "published_at": "2026-08-16 09:00:00",
            "web_url": web_url,
        }

    def test_rendered_title_and_cta_never_point_at_media(self):
        for media in MEDIA_SAMPLES:
            html = email_out.render([self.pick(media)], "unsub-token")
            self.assertNotIn(media, html, f"{media} reached the rendered digest")
            self.assertNotIn("href=\"https://cdn.example.com", html)
            self.assertIn("Episode 267", html)  # the title still renders

    def test_readable_page_is_linked_from_both_title_and_cta(self):
        html = email_out.render([self.pick(EPISODE_PAGE)], "unsub-token")
        self.assertEqual(html.count(EPISODE_PAGE), 2)
        self.assertIn("View episode", html)

    def test_missing_link_degrades_to_plain_title_with_no_button(self):
        html = email_out.render([self.pick("")], "unsub-token")
        self.assertIn("Episode 267", html)
        self.assertNotIn("View episode", html)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "repair.db"
        db.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, conn, guid: str, web_url: str) -> int:
        return conn.execute(
            "INSERT INTO episode (guid, feed_id, show_name, title, description, "
            "web_url) VALUES (?, 1, 'Show', 'Title', 'description', ?)",
            (guid, web_url),
        ).lastrowid

    def test_repair_clears_only_media_links_and_is_idempotent(self):
        with db.session(self.path) as conn:
            audio = self.add(conn, "audio", AUDIO)
            tracked = self.add(conn, "tracked", AUDIO_TRACKED)
            page = self.add(conn, "page", EPISODE_PAGE)
            blank = self.add(conn, "blank", "")

            scanned, cleared = fetch.repair_media_links(conn)
            self.assertEqual((scanned, cleared), (3, 2))

            urls = {
                row["id"]: row["web_url"]
                for row in conn.execute("SELECT id, web_url FROM episode").fetchall()
            }
            self.assertEqual(urls[audio], "")
            self.assertEqual(urls[tracked], "")
            self.assertEqual(urls[page], EPISODE_PAGE)
            self.assertEqual(urls[blank], "")

            # Second pass finds nothing left to clean.
            self.assertEqual(fetch.repair_media_links(conn), (1, 0))

    def test_repaired_row_cannot_be_emailed_as_a_link(self):
        with db.session(self.path) as conn:
            self.add(conn, "audio", AUDIO)
            fetch.repair_media_links(conn)
            row = conn.execute(
                "SELECT web_url FROM episode WHERE guid='audio'"
            ).fetchone()
        self.assertEqual(links.safe_page_url(row["web_url"]), "")


if __name__ == "__main__":
    unittest.main()
