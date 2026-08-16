"""Discover recent category feeds, then fetch their episodes in ID batches.

Two stages, deliberately separate.

**Stage 1, discover and fetch.** Stamp `run.fetch_cutoff_at`, query recently
updated feeds across the 20 mapped categories, balance them to the daily token
budget, then fetch episodes with up to 200 feed IDs per API request. Upsert into
`episode` on `guid`.

The window is the last good `run.fetch_cutoff_at`, never "the last two days".
A fixed window silently drops episodes whenever a run fails; using the last
successful/partial run means a missed Wednesday gets picked up on Friday. It is
capped at `MAX_LOOKBACK_DAYS` so a long gap produces a digest, not a flood.

**Stage 2, filter.** Cheap, deterministic, no LLM: drop descriptions too thin
to judge and anything too short to be an episode. There is deliberately no
already-sent join here: `sent` is per subscriber, while this pool is shared.
One subscriber's history must never hide an episode from another.
"""

import argparse
import csv
import html
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from _shared import config, db, links
import discover
import podcastindex

# Episode types the catalogue marks as not-an-episode. Dropped before the
# upsert: a trailer is useless to every user, so it does not belong in a
# cache that is shared across all of them.
SKIP_EPISODE_TYPES = frozenset({"trailer", "bonus"})

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Chunk size for `WHERE guid IN (...)`. Well under SQLite's variable limit.
_CHUNK = 400


class FetchError(RuntimeError):
    """Fetching failed in a way that makes the run's output untrustworthy."""


@dataclass
class NewEpisode:
    guid: str
    feed_id: int
    show_name: str
    title: str
    description: str
    duration_sec: int | None
    published_at: str | None
    web_url: str


@dataclass
class FetchResult:
    run_id: int
    since: int
    shows: int
    discovered: int = 0
    raw: int = 0
    stored: int = 0
    candidates: list[sqlite3.Row] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)
    feed_errors: int = 0

    @property
    def after_filter(self) -> int:
        return len(self.candidates)


# --- the window --------------------------------------------------------------


def since_timestamp(last_run_at: str | None, max_days: int | None = None) -> int:
    """Unix seconds to fetch from: the last successful run, floored at the cap.

    The stored cutoff is written by SQLite's `datetime('now')`, which is UTC
    with no timezone marker. Parsing it as local time would shift the window
    and quietly lose or repeat episodes.
    """
    days = config.MAX_LOOKBACK_DAYS if max_days is None else max_days
    floor = datetime.now(timezone.utc) - timedelta(days=days)

    if last_run_at:
        try:
            parsed = datetime.fromisoformat(last_run_at)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            floor = max(floor, parsed)

    return int(floor.timestamp())


# --- stage 1: fetch ----------------------------------------------------------


def clean_text(value: str | None) -> str:
    """Strip markup and collapse whitespace.

    Feeds put HTML in `description`. Left in, tags would count toward
    `MIN_DESC_CHARS` — a two-line description wrapped in markup would clear a
    100-character bar on tags alone — and would eat the ranker's 400-character
    budget without carrying signal.
    """
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def to_episode(item: dict, fallback_show: str) -> NewEpisode | None:
    """Map one API item. Returns None for anything that is not an episode."""
    guid = (item.get("guid") or "").strip()
    if not guid:
        return None  # nothing to dedupe on; the one field we cannot do without
    if (item.get("episodeType") or "").lower() in SKIP_EPISODE_TYPES:
        return None

    published = item.get("datePublished")
    duration = item.get("duration")

    return NewEpisode(
        guid=guid,
        feed_id=int(item.get("feedId") or 0),
        show_name=clean_text(item.get("feedTitle")) or fallback_show,
        title=clean_text(item.get("title")) or "(untitled)",
        description=clean_text(item.get("description")),
        duration_sec=int(duration) if duration else None,
        published_at=(
            datetime.fromtimestamp(published, timezone.utc).isoformat(timespec="seconds")
            if published
            else None
        ),
        # Never `enclosureUrl`: that is the audio file, and roughly half of
        # recent episodes carry no `link`, so an "or" fallback made the raw
        # media URL the majority case. See block-1-setup/links.py.
        web_url=links.episode_page_url(
            item.get("link"),
            itunes_id=item.get("feedItunesId"),
            enclosure_url=item.get("enclosureUrl"),
        ),
    )


def _show_value(show, key: str):
    if isinstance(show, (sqlite3.Row, dict)):
        return show[key]
    if key == "show_name":
        return show.title
    return getattr(show, key)


def fetch_feeds(
    shows: list,
    since: int,
    progress=None,
) -> tuple[list[NewEpisode], int, int]:
    """Fetch up to 200 shows per request. Returns episodes, raw, failed feeds.

    One dead feed must not take down the run, so per-feed failures are counted
    rather than raised. Every feed failing is a different thing entirely — that
    is a bad key or no network, and it is indistinguishable from a quiet week
    unless it raises.
    """
    limits = httpx.Limits(max_connections=config.FETCH_BATCH_WORKERS)
    episodes: list[NewEpisode] = []
    raw = 0
    failed = 0

    with httpx.Client(timeout=podcastindex.TIMEOUT, limits=limits) as client:

        batches = [
            shows[start : start + config.EPISODE_FEED_BATCH_SIZE]
            for start in range(0, len(shows), config.EPISODE_FEED_BATCH_SIZE)
        ]

        def poll(batch: list) -> list[dict]:
            return podcastindex.episodes_by_feed_ids(
                [_show_value(show, "feed_id") for show in batch],
                since=since,
                max_results=config.EPISODE_BATCH_MAX_RESULTS,
                client=client,
            )

        show_names = {
            _show_value(show, "feed_id"): _show_value(show, "show_name")
            for show in shows
        }
        with ThreadPoolExecutor(max_workers=config.FETCH_BATCH_WORKERS) as pool:
            futures = {pool.submit(_safe(poll), batch): batch for batch in batches}
            completed = 0
            for future in as_completed(futures):
                batch = futures[future]
                items = future.result()
                if items is None:
                    failed += len(batch)
                else:
                    raw += len(items)
                    for item in items:
                        fallback = show_names.get(int(item.get("feedId") or 0), "")
                        episode = to_episode(item, fallback)
                        if episode is not None:
                            episodes.append(episode)
                completed += len(batch)
                if progress is not None:
                    progress(completed, len(shows), raw, failed)

    if shows and failed == len(shows):
        raise FetchError(
            f"All {failed} feeds failed. Check the network and PODCASTINDEX_* keys — "
            "a zero-candidate run otherwise looks exactly like a quiet week."
        )
    return episodes, raw, failed


def _safe(fn):
    def wrapped(show):
        try:
            return fn(show)
        except (podcastindex.PodcastIndexError, httpx.HTTPError):
            return None

    return wrapped


def upsert_episodes(conn, episodes: list[NewEpisode]) -> list[int]:
    """Upsert on `guid` and return the row ids.

    Dedupe on guid, never on title (ARCHITECTURE section 5). Feeds republish
    episodes with edited titles constantly, and title-based dedupe means sending
    the same episode twice.
    """
    if not episodes:
        return []

    db.execute_values(
        conn,
        """
        INSERT INTO episode
            (guid, feed_id, show_name, title, description, duration_sec, published_at, web_url)
        VALUES {values}
        ON CONFLICT (guid) DO UPDATE SET
            title        = excluded.title,
            description  = excluded.description,
            duration_sec = excluded.duration_sec,
            published_at = excluded.published_at,
            web_url      = excluded.web_url
        """,
        [
            (
                e.guid, e.feed_id, e.show_name, e.title,
                e.description, e.duration_sec, e.published_at, e.web_url,
            )
            for e in episodes
        ],
    )

    guids = list({e.guid for e in episodes})
    ids: list[int] = []
    for start in range(0, len(guids), _CHUNK):
        batch = guids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in batch)
        ids += [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM episode WHERE guid IN ({placeholders})", batch
            )
        ]
    return ids


def repair_media_links(conn, chunk: int = _CHUNK) -> tuple[int, int]:
    """Blank stored links that point at audio files. Returns (scanned, cleared).

    Rows written before the link fix used `enclosureUrl` whenever a feed
    omitted `link`, which was most episodes. Those rows are still taggable and
    still curatable, so the pipeline must not be able to email them.

    There is no episode page to recover offline: the feed's `link` was empty
    and the iTunes id was never stored, so the honest repair is to blank the
    column and let the digest print an unlinked title. Any episode re-seen
    inside a later fetch window is repopulated correctly by `upsert_episodes`.

    Idempotent, and safe to run before every digest.
    """
    rows = conn.execute(
        "SELECT id, web_url FROM episode WHERE web_url IS NOT NULL AND web_url <> ''"
    ).fetchall()
    broken = [row["id"] for row in rows if not links.is_page_url(row["web_url"])]
    for start in range(0, len(broken), chunk):
        batch = broken[start : start + chunk]
        placeholders = ",".join("?" for _ in batch)
        conn.execute(
            f"UPDATE episode SET web_url = '' WHERE id IN ({placeholders})", batch
        )
    conn.commit()
    return len(rows), len(broken)


# --- stage 2: filter ---------------------------------------------------------


def filter_episodes(episodes: list[NewEpisode]) -> tuple[list[NewEpisode], Counter]:
    """Apply the global cheap rules and account for every episode dropped.

    This runs before the upsert because every row with `tagged_at IS NULL` is
    tagger work. Storing a 20-character description and merely hiding it from
    this function's return value would still spend LLM tokens on it in Step 5.
    """
    dropped: Counter = Counter()
    kept: list[NewEpisode] = []
    seen: set[str] = set()

    for episode in episodes:
        if episode.guid in seen:
            dropped["duplicate guid"] += 1
        elif len(episode.description) < config.MIN_DESC_CHARS:
            dropped[f"description under {config.MIN_DESC_CHARS} chars"] += 1
        elif (
            episode.duration_sec is not None
            and episode.duration_sec < config.MIN_EPISODE_SEC
        ):
            dropped[f"under {config.MIN_EPISODE_SEC // 60} minutes"] += 1
        else:
            seen.add(episode.guid)
            kept.append(episode)

    kept.sort(key=lambda episode: episode.published_at or "", reverse=True)
    return kept, dropped


# --- the block's entry point -------------------------------------------------


def load_shows(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT feed_id, feed_url, title AS show_name FROM show "
        "WHERE status = 'active' ORDER BY id"
    ).fetchall()


def fetch_all(
    conn,
    since: int | None = None,
    run_id: int | None = None,
    progress=None,
    refresh_discovery: bool = True,
    discovery_topics: tuple[str, ...] | None = None,
    discovery_target: int | None = None,
    candidate_limit: int | None = None,
) -> FetchResult:
    """Fetch the shared universe and leave taggable rows in `episode`.

    `fetch_cutoff_at` is committed before the first outbound call. The commit
    is intentional: if the process dies while polling, the attempt record must
    still exist. A failed/running row does not advance `last_good_cutoff()`, so
    the next complete run safely re-covers the same window.
    """
    if run_id is None:
        cursor = conn.execute("INSERT INTO run DEFAULT VALUES")
        run_id = cursor.lastrowid
    elif conn.execute("SELECT 1 FROM run WHERE id = ?", (run_id,)).fetchone() is None:
        raise FetchError(f"No run with id {run_id}.")

    previous_cutoff = db.last_good_cutoff(conn)
    window = since_timestamp(previous_cutoff) if since is None else since
    fetch_cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE run SET fetch_cutoff_at = ? WHERE id = ?",
        (fetch_cutoff, run_id),
    )
    conn.commit()

    discovered_count = 0
    if refresh_discovery:
        discovery = discover.discover_recent(
            window,
            topic_slugs=discovery_topics,
            target=discovery_target,
        )
        # Category discovery is an external HTTP phase. A committed Turso
        # connection may lose its idle stream while those requests run.
        db.ensure_connection(conn)
        discovered_count = discovery.found
        discover.save_discovered(conn, discovery.selected)
        conn.commit()
        shows = discover.exclude_muted(conn, discovery.selected)
    else:
        shows = load_shows(conn)

    result = FetchResult(
        run_id=run_id,
        since=window,
        shows=len(shows),
        discovered=discovered_count,
    )
    if not shows:
        return result

    episodes, result.raw, result.feed_errors = fetch_feeds(shows, window, progress=progress)
    # Resume database work through a retry-safe read; never retry an episode
    # write whose server-side outcome could be uncertain.
    db.ensure_connection(conn)
    candidates, result.dropped = filter_episodes(episodes)
    if candidate_limit is not None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        candidate_guids = list({episode.guid for episode in candidates})
        known: set[str] = set()
        processable: set[str] = set()
        for start in range(0, len(candidate_guids), _CHUNK):
            batch = candidate_guids[start : start + _CHUNK]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT guid, tagged_at, tag_attempts FROM episode "
                f"WHERE guid IN ({placeholders})",
                batch,
            ).fetchall()
            known.update(row["guid"] for row in rows)
            processable.update(
                row["guid"]
                for row in rows
                if row["tagged_at"] is None
                and row["tag_attempts"] < config.TAG_MAX_ATTEMPTS
            )
        candidates = [
            episode
            for episode in candidates
            if episode.guid not in known or episode.guid in processable
        ]
        dropped_by_cap = max(0, len(candidates) - candidate_limit)
        if dropped_by_cap:
            result.dropped["short digest episode cap"] += dropped_by_cap
        candidates = candidates[:candidate_limit]
    guids = {episode.guid for episode in candidates}
    existing: set[str] = set()
    guid_list = list(guids)
    for start in range(0, len(guid_list), _CHUNK):
        batch = guid_list[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in batch)
        existing.update(
            row["guid"]
            for row in conn.execute(
                f"SELECT guid FROM episode WHERE guid IN ({placeholders})", batch
            ).fetchall()
        )

    ids = upsert_episodes(conn, candidates)
    result.stored = len(guids - existing)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        result.candidates = conn.execute(
            f"SELECT * FROM episode WHERE id IN ({placeholders}) "
            "ORDER BY published_at DESC",
            ids,
        ).fetchall()
    return result


def export_episodes(path: Path, rows: list[sqlite3.Row]) -> int:
    """Save the exact post-filter pool from this run for human inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "guid", "feed_id", "show_name", "title", "description",
        "duration_sec", "published_at", "web_url",
    )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows([[row[field] for field in fields] for row in rows])
    return len(rows)


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--days",
        type=int,
        help=f"override the last-good cutoff (cap {config.MAX_LOOKBACK_DAYS})",
    )
    parser.add_argument("--show", type=int, default=10, help="how many titles to print")
    parser.add_argument("--export", type=Path, help="save this run's filtered episodes to CSV")
    args = parser.parse_args()

    started = time.time()

    def show_progress(completed, total, raw, failed):
        if completed % 100 == 0 or completed == total:
            print(
                f"polled {completed}/{total} shows, {raw} raw episodes, "
                f"{failed} feed errors",
                flush=True,
            )

    with db.session() as conn:
        cursor = conn.execute("INSERT INTO run DEFAULT VALUES")
        run_id = cursor.lastrowid
        since = since_timestamp(None, max_days=args.days) if args.days else None
        try:
            result = fetch_all(conn, since=since, run_id=run_id, progress=show_progress)
        except Exception:
            conn.execute(
                "UPDATE run SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
                (run_id,),
            )
            conn.commit()
            raise
        conn.execute(
            "UPDATE run SET fetched = ?, status = 'ok', "
            "finished_at = datetime('now') WHERE id = ?",
            (result.stored, run_id),
        )

    window = datetime.fromtimestamp(result.since, timezone.utc)
    age = (time.time() - result.since) / 86400
    print(
        f"{result.discovered} recent category feeds found, {result.shows} selected; "
        f"since {window:%Y-%m-%d %H:%M} UTC "
        f"({age:.1f}d ago)  [{time.time() - started:.1f}s]"
    )
    if result.feed_errors:
        print(f"warn  {result.feed_errors} feeds failed to respond")

    # THE CHECK.
    print(
        f"\n{result.raw} raw -> {result.after_filter} after filter "
        f"-> {result.stored} new"
    )
    for reason, count in result.dropped.most_common():
        print(f"  -{count:>5}  {reason}")

    for row in result.candidates[: args.show]:
        mins = f"{row['duration_sec'] // 60}m" if row["duration_sec"] else " ?"
        date = (row["published_at"] or "")[:10]
        print(f"\n  {date}  {mins:>5}  {row['show_name'][:44]}")
        print(f"           {row['title'][:70]}")

    if args.export:
        written = export_episodes(args.export, result.candidates)
        print(f"\nwrote {written} filtered episodes to {args.export}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
