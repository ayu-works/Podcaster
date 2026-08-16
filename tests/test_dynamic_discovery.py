"""Regression gates for the category-discovery architecture pivot."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-4-fetch"))
sys.path.insert(0, str(ROOT / "block-2-universe"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import discover  # noqa: E402
import fetch  # noqa: E402
import podcastindex  # noqa: E402


class DynamicDiscoveryTests(unittest.TestCase):
    def test_every_product_topic_has_official_categories(self):
        self.assertEqual(set(config.TOPIC_CATEGORIES), set(config.TOPIC_SLUGS))
        self.assertTrue(
            all(config.TOPIC_CATEGORIES[slug] for slug in config.TOPIC_SLUGS)
        )
        self.assertTrue(
            all(
                isinstance(category, int) and 1 <= category <= 112
                for categories in config.TOPIC_CATEGORIES.values()
                for category in categories
            )
        )

    def test_recent_discovery_deduplicates_and_balances_topics(self):
        categories_to_index = {
            categories: index
            for index, categories in enumerate(config.TOPIC_CATEGORIES.values())
        }

        def recent(since, categories, max_results=None, client=None, **kwargs):
            index = categories_to_index[categories]
            feeds = [
                {
                    "id": index * 100 + offset + 1,
                    "url": f"https://example.com/{index}-{offset}.xml",
                    "title": f"Topic {index} Show {offset}",
                    "newestItemPubdate": 10_000 - offset,
                }
                for offset in range(5)
            ]
            if index in (0, 1):
                feeds.append(
                    {
                        "id": 99_999,
                        "url": "https://example.com/shared.xml",
                        "title": "Shared",
                        "newestItemPubdate": 20_000,
                    }
                )
            return feeds

        with (
            patch.object(discover.podcastindex, "recent_feeds", side_effect=recent),
            patch.object(config, "DISCOVERY_FEED_TARGET", 40),
        ):
            result = discover.discover_recent(123)

        self.assertEqual(result.found, 101)
        self.assertEqual(len(result.selected), 40)
        self.assertEqual(len({feed.feed_id for feed in result.selected}), 40)
        self.assertGreater(min(result.counts_by_topic.values()), 0)
        shared = next(feed for feed in result.selected if feed.feed_id == 99_999)
        self.assertEqual(shared.matched_topics, {0, 1})

    def test_discovery_cache_preserves_mutes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "discover.db"
            db.init_db(path)
            feeds = [
                discover.DiscoveredFeed(1, "https://one.xml", "One", 10, {0}),
                discover.DiscoveredFeed(2, "https://two.xml", "Two", 20, {1}),
            ]
            with db.session(path) as conn:
                discover.save_discovered(conn, feeds)
                conn.execute("UPDATE show SET status='muted' WHERE feed_id=1")
            with db.session(path) as conn:
                discover.save_discovered(conn, feeds)
                active = discover.exclude_muted(conn, feeds)
                status = conn.execute(
                    "SELECT status FROM show WHERE feed_id=1"
                ).fetchone()[0]
            self.assertEqual(status, "muted")
            self.assertEqual([feed.feed_id for feed in active], [2])

    def test_episode_api_joins_up_to_200_ids(self):
        with patch.object(podcastindex, "_get", return_value={"items": []}) as get:
            podcastindex.episodes_by_feed_ids(list(range(1, 201)), since=123)
        path, params, _client = get.call_args.args
        self.assertEqual(path, "/episodes/byfeedid")
        self.assertEqual(len(params["id"].split(",")), 200)
        self.assertEqual(params["since"], 123)
        self.assertEqual(params["max"], config.EPISODE_BATCH_MAX_RESULTS)
        with self.assertRaises(ValueError):
            podcastindex.episodes_by_feed_ids(list(range(201)))

    def test_fetch_uses_three_requests_for_450_feeds(self):
        shows = [
            {"feed_id": index, "show_name": f"Show {index}"}
            for index in range(1, 451)
        ]
        batch_sizes: list[int] = []

        def episodes(feed_ids, **kwargs):
            batch_sizes.append(len(feed_ids))
            return []

        with patch.object(fetch.podcastindex, "episodes_by_feed_ids", side_effect=episodes):
            rows, raw, failed = fetch.fetch_feeds(shows, since=123)
        self.assertEqual(sorted(batch_sizes), [50, 200, 200])
        self.assertEqual((rows, raw, failed), ([], 0, 0))

    def test_fetch_refreshes_from_dynamic_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fetch.db"
            db.init_db(path)
            selected = [
                discover.DiscoveredFeed(7, "https://seven.xml", "Seven", 10, {0})
            ]
            discovery = discover.DiscoveryResult(
                found=12,
                selected=selected,
                counts_by_topic={slug: 0 for slug in config.TOPIC_SLUGS},
            )
            with db.session(path) as conn:
                run_id = conn.execute("INSERT INTO run DEFAULT VALUES").lastrowid
                with (
                    patch.object(fetch.discover, "discover_recent", return_value=discovery),
                    patch.object(fetch, "fetch_feeds", return_value=([], 0, 0)),
                    patch.object(
                        fetch.db,
                        "ensure_connection",
                        wraps=fetch.db.ensure_connection,
                    ) as refresh,
                ):
                    result = fetch.fetch_all(conn, since=123, run_id=run_id)
            self.assertEqual(result.discovered, 12)
            self.assertEqual(result.shows, 1)
            self.assertEqual(refresh.call_count, 2)


if __name__ == "__main__":
    unittest.main()
