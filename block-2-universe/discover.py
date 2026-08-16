"""Discover recently updated shows by category for each pipeline run."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

import podcastindex
from _shared import config


class DiscoveryError(RuntimeError):
    """Recent category discovery could not produce a trustworthy result."""


@dataclass
class DiscoveredFeed:
    feed_id: int
    feed_url: str
    title: str
    updated_at: int
    matched_topics: set[int] = field(default_factory=set)


@dataclass
class DiscoveryResult:
    found: int
    selected: list[DiscoveredFeed]
    counts_by_topic: dict[str, int]


def _feed_value(feed: dict, *names, default=None):
    for name in names:
        value = feed.get(name)
        if value is not None:
            return value
    return default


def balance(feeds: list[DiscoveredFeed], target: int) -> list[DiscoveredFeed]:
    """Round-robin recent feeds so broad categories cannot monopolise."""
    ranked = sorted(feeds, key=lambda feed: feed.updated_at, reverse=True)
    queues = [
        [feed for feed in ranked if topic_index in feed.matched_topics]
        for topic_index in range(len(config.TOPICS))
    ]
    cursors = [0] * len(queues)
    selected: list[DiscoveredFeed] = []
    taken: set[int] = set()
    while len(selected) < target:
        progress = False
        for topic_index, queue in enumerate(queues):
            cursor = cursors[topic_index]
            while cursor < len(queue) and queue[cursor].feed_id in taken:
                cursor += 1
            cursors[topic_index] = cursor
            if cursor < len(queue):
                feed = queue[cursor]
                selected.append(feed)
                taken.add(feed.feed_id)
                cursors[topic_index] += 1
                progress = True
                if len(selected) == target:
                    break
        if not progress:
            break
    return selected


def discover_recent(since: int, client: httpx.Client | None = None) -> DiscoveryResult:
    """Query every product topic, merge feed IDs, then balance to the budget."""
    owned = client is None
    client = client or httpx.Client(timeout=podcastindex.TIMEOUT)
    try:
        def query(index_and_topic):
            index, (slug, _label) = index_and_topic
            categories = config.TOPIC_CATEGORIES[slug]
            feeds = podcastindex.recent_feeds(
                since,
                categories,
                max_results=config.DISCOVERY_RESULTS_PER_TOPIC,
                client=client,
            )
            return index, feeds

        with ThreadPoolExecutor(max_workers=config.DISCOVERY_WORKERS) as pool:
            responses = list(pool.map(query, enumerate(config.TOPICS)))
    except Exception as exc:
        raise DiscoveryError("Podcast Index recent-category discovery failed") from exc
    finally:
        if owned:
            client.close()

    merged: dict[int, DiscoveredFeed] = {}
    raw_counts: dict[str, int] = {}
    for topic_index, raw_feeds in responses:
        slug = config.TOPIC_SLUGS[topic_index]
        raw_counts[slug] = len(raw_feeds)
        for raw in raw_feeds:
            feed_id = _feed_value(raw, "id", "feedId")
            feed_url = _feed_value(raw, "url", "feedUrl", default="")
            if not feed_id or not feed_url:
                continue
            updated = int(
                _feed_value(
                    raw,
                    "newestItemPubdate",
                    "lastUpdateTime",
                    "lastCrawlTime",
                    default=0,
                )
                or 0
            )
            current = merged.get(int(feed_id))
            if current is None:
                current = DiscoveredFeed(
                    feed_id=int(feed_id),
                    feed_url=str(feed_url),
                    title=str(_feed_value(raw, "title", default=f"feed {feed_id}")).strip(),
                    updated_at=updated,
                )
                merged[current.feed_id] = current
            current.matched_topics.add(topic_index)
            if updated > current.updated_at:
                current.updated_at = updated

    selected = balance(list(merged.values()), config.DISCOVERY_FEED_TARGET)
    counts = {
        slug: sum(index in feed.matched_topics for feed in selected)
        for index, slug in enumerate(config.TOPIC_SLUGS)
    }
    return DiscoveryResult(found=len(merged), selected=selected, counts_by_topic=counts)


def save_discovered(conn, feeds: list[DiscoveredFeed]) -> None:
    """Upsert the discovery cache while retaining prior mute decisions."""
    if not feeds:
        return
    conn.executemany(
        """
        INSERT INTO show (feed_id, feed_url, title)
        VALUES (?, ?, ?)
        ON CONFLICT (feed_id) DO UPDATE SET
            feed_url = excluded.feed_url,
            title = excluded.title
        """,
        [(feed.feed_id, feed.feed_url, feed.title) for feed in feeds],
    )
    feed_ids = [feed.feed_id for feed in feeds]
    placeholders = ",".join("?" for _ in feed_ids)
    ids = {
        row["feed_id"]: row["id"]
        for row in conn.execute(
            f"SELECT id, feed_id FROM show WHERE feed_id IN ({placeholders})", feed_ids
        ).fetchall()
    }
    conn.executemany(
        "INSERT OR IGNORE INTO show_topic (show_id, topic) VALUES (?, ?)",
        [
            (ids[feed.feed_id], config.TOPIC_SLUGS[index])
            for feed in feeds
            for index in feed.matched_topics
        ],
    )


def exclude_muted(conn, feeds: list[DiscoveredFeed]) -> list[DiscoveredFeed]:
    if not feeds:
        return []
    feed_ids = [feed.feed_id for feed in feeds]
    placeholders = ",".join("?" for _ in feed_ids)
    muted = {
        row["feed_id"]
        for row in conn.execute(
            f"SELECT feed_id FROM show WHERE status = 'muted' "
            f"AND feed_id IN ({placeholders})",
            feed_ids,
        ).fetchall()
    }
    return [feed for feed in feeds if feed.feed_id not in muted]
