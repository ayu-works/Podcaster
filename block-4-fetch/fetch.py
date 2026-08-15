"""Pull new episodes from the user's shows, then filter them down (ARCHITECTURE section 6).

Two stages, deliberately separate.

**Stage 1, fetch.** Ask each of the ~200 candidate shows for episodes published
since `user.last_run_at`, 15 feeds at a time. Upsert into `episode` on `guid`.

The window is `last_run_at`, never "the last two days". A fixed window silently
drops episodes whenever a run fails or the laptop was asleep; using the last
successful run means a missed Wednesday gets picked up on Friday. It is capped
at `MAX_LOOKBACK_DAYS` so a long gap produces a digest, not a flood.

**Stage 2, filter.** Cheap, deterministic, no LLM: drop anything already sent to
this user, anything with a description too thin to judge, and anything too short
to be an episode. The description rule does the most work — the ranker cannot
judge what it cannot read, and thin descriptions are the largest single source
of bad picks.

The "already sent" rule lives here, before the ranker ever sees a candidate.
The LLM is never responsible for remembering (ARCHITECTURE section 5).
"""

import argparse
import html
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from _shared import config, db
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
    since: int
    shows: int
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

    `last_run_at` is written by SQLite's `datetime('now')`, which is UTC with no
    timezone marker. Parsing it as naive local time would shift the window by
    hours and quietly lose or repeat episodes.
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
        web_url=item.get("link") or item.get("enclosureUrl") or "",
    )


def fetch_feeds(shows: list[sqlite3.Row], since: int) -> tuple[list[NewEpisode], int, int]:
    """Poll every show concurrently. Returns (episodes, raw_items, failed_feeds).

    One dead feed must not take down the run, so per-feed failures are counted
    rather than raised. Every feed failing is a different thing entirely — that
    is a bad key or no network, and it is indistinguishable from a quiet week
    unless it raises.
    """
    limits = httpx.Limits(max_connections=config.FEED_WORKERS)
    episodes: list[NewEpisode] = []
    raw = 0
    failed = 0

    with httpx.Client(timeout=podcastindex.TIMEOUT, limits=limits) as client:

        def poll(show: sqlite3.Row) -> list[dict]:
            return podcastindex.episodes_by_feed(
                show["feed_id"],
                since=since,
                max_results=config.EPISODES_PER_FEED,
                client=client,
            )

        with ThreadPoolExecutor(max_workers=config.FEED_WORKERS) as pool:
            for show, items in zip(shows, pool.map(_safe(poll), shows)):
                if items is None:
                    failed += 1
                    continue
                raw += len(items)
                for item in items:
                    episode = to_episode(item, show["show_name"])
                    if episode is not None:
                        episodes.append(episode)

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

    conn.executemany(
        """
        INSERT INTO episode
            (guid, feed_id, show_name, title, description, duration_sec, published_at, web_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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


# --- stage 2: filter ---------------------------------------------------------


def filter_episodes(
    conn, user_id: int, episode_ids: list[int]
) -> tuple[list[sqlite3.Row], Counter]:
    """Apply the four cheap rules, and account for every episode dropped.

    The counts matter as much as the survivors: `after_filter` collapsing is
    the signal that the universe is too narrow, and from the outside that looks
    identical to a quiet week (ARCHITECTURE section 10).
    """
    dropped: Counter = Counter()
    kept: list[sqlite3.Row] = []
    seen: set[str] = set()

    for start in range(0, len(episode_ids), _CHUNK):
        batch = episode_ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT e.*,
                   EXISTS (
                       SELECT 1 FROM digest_item di
                       JOIN digest d ON d.id = di.digest_id
                       WHERE di.episode_id = e.id AND d.user_id = ?
                         AND d.kind IN ('sent', 'pending')
                   ) AS already_sent
            FROM episode e
            WHERE e.id IN ({placeholders})
            ORDER BY e.published_at DESC
            """,
            [user_id, *batch],
        ).fetchall()

        for row in rows:
            if row["already_sent"]:
                dropped["already sent"] += 1
            elif row["guid"] in seen:
                dropped["duplicate guid"] += 1
            elif len(row["description"] or "") < config.MIN_DESC_CHARS:
                dropped[f"description under {config.MIN_DESC_CHARS} chars"] += 1
            elif row["duration_sec"] is not None and row["duration_sec"] < config.MIN_EPISODE_SEC:
                dropped[f"under {config.MIN_EPISODE_SEC // 60} minutes"] += 1
            else:
                seen.add(row["guid"])
                kept.append(row)

    kept.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return kept, dropped


# --- the block's entry point -------------------------------------------------


def load_shows(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT feed_id, feed_url, show_name FROM candidate_show "
        "WHERE user_id = ? AND status = 'active' ORDER BY id",
        (user_id,),
    ).fetchall()


def fetch_for_user(conn, user_id: int, since: int | None = None) -> FetchResult:
    """Everything Block 5 needs: fresh episodes, filtered, newest first.

    Does **not** advance `user.last_run_at`. That belongs to the run job, after
    a successful delivery — fetching and then failing must not consume the
    window.
    """
    row = conn.execute("SELECT last_run_at FROM user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise FetchError(f"No user with id {user_id}.")

    shows = load_shows(conn, user_id)
    if not shows:
        raise FetchError(
            f"User {user_id} has no active candidate shows. Run Block 2 or the "
            "onboarding form first — there is nothing to poll."
        )

    window = since_timestamp(row["last_run_at"]) if since is None else since
    result = FetchResult(since=window, shows=len(shows))

    episodes, result.raw, result.feed_errors = fetch_feeds(shows, window)
    ids = upsert_episodes(conn, episodes)
    result.stored = len(ids)
    result.candidates, result.dropped = filter_episodes(conn, user_id, ids)
    return result


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", required=True, help="whose shows to poll")
    parser.add_argument(
        "--days",
        type=int,
        help=f"override the window, ignoring last_run_at (cap {config.MAX_LOOKBACK_DAYS})",
    )
    parser.add_argument("--show", type=int, default=10, help="how many titles to print")
    args = parser.parse_args()

    started = time.time()
    with db.session() as conn:
        user = conn.execute(
            "SELECT id, last_run_at FROM user WHERE email = ?", (args.email,)
        ).fetchone()
        if user is None:
            print(f"No user {args.email}. Subscribe first (Block 3).", file=sys.stderr)
            return 1

        since = since_timestamp(None, max_days=args.days) if args.days else None
        result = fetch_for_user(conn, user["id"], since=since)

    window = datetime.fromtimestamp(result.since, timezone.utc)
    age = (time.time() - result.since) / 86400
    print(
        f"{result.shows} shows, since {window:%Y-%m-%d %H:%M} UTC "
        f"({age:.1f}d ago)  [{time.time() - started:.1f}s]"
    )
    if result.feed_errors:
        print(f"warn  {result.feed_errors} feeds failed to respond")

    # THE CHECK.
    print(f"\n{result.raw} raw -> {result.after_filter} after filter")
    for reason, count in result.dropped.most_common():
        print(f"  -{count:>5}  {reason}")

    for row in result.candidates[: args.show]:
        mins = f"{row['duration_sec'] // 60}m" if row["duration_sec"] else " ?"
        date = (row["published_at"] or "")[:10]
        print(f"\n  {date}  {mins:>5}  {row['show_name'][:44]}")
        print(f"           {row['title'][:70]}")

    if result.after_filter < 30:
        print(
            f"\nSTOP. Only {result.after_filter} candidates. This is a thin pool, and it "
            "produces bad picks that look exactly like a bad ranker.\n"
            "Widen the universe first — more terms per interest, or a bigger "
            "UNIVERSE_TARGET — before touching Block 5."
        )
        return 1
    if result.after_filter < 60:
        print(f"\nwarn  {result.after_filter} candidates; 60+ is what you want.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
