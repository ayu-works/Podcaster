"""Build a static reference/fallback show universe.

The scheduled product no longer reads this list: `discover.py` queries recent
official categories every run. This tool and its CSV remain useful for manual
catalogue comparisons and as an explicit fallback if the recent-feed endpoint
ever changes.

The interest list is `config.TOPICS`, not one user's free text — v1's
per-signup universe build is gone; signup no longer builds anything. One
Groq call per batch expands topic labels into catalogue-style search terms;
see INTERESTS_PER_EXPANSION_BATCH below for why 20 topics can't go in one
call the way v1's three free-text interests did.
"""

import argparse
import csv
import json
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from groq import APIStatusError, Groq, RateLimitError

import podcastindex
from _shared import config, db

# Reciprocal-rank fusion constant. Merges the per-term ranked lists into one
# ordering without needing a comparable relevance score across searches; k
# damps the pull of any single term's top hit. 60 is the conventional value.
RRF_K = 60

# expand_interests() sends every interest it's given in ONE Groq call, and
# was sized for v1's three free-text interests (3 * TERMS_PER_INTEREST = 54
# terms), comfortably inside its max_completion_tokens=2500. Twenty topics
# need 20 * 18 = 360 terms in one response, which will not fit — so this
# batches, keeping max_completion_tokens=2500 untouched per call.
#
# Arithmetic: the response shape is {"index":N,"terms":["term one",...]}.
# The expansion prompt requires each term to be 1-3 words, so call it up to
# ~15 tokens/term once quoting, the comma separator and BPE splitting on
# short catalogue phrases are accounted for — a deliberately generous
# ceiling, not a measured average, because there's no cheap way to probe
# actual output size before making the call, and the validator below fails
# the WHOLE batch if any one interest comes back short. 18 terms/interest *
# 15 tokens/term = 270 terms tokens, plus ~20 tokens of the
# {"index":...,"terms":[...]} wrapper itself: ~290 tokens/interest of
# output. The v1 call was measured with three interests. A live five-topic
# call failed Groq's server-side JSON validation even with nominal token
# headroom, so keep the proven three-topic contract rather than trusting
# arithmetic alone. Twenty topics therefore take seven monthly calls.
INTERESTS_PER_EXPANSION_BATCH = 3

# Each three-topic batch is roughly 1,500-1,700 tokens all-in. A 20-second
# pause holds the seven monthly calls near 5,100 tokens/minute, comfortably
# below GROQ_TPM without a rolling-window rate limiter for this small job.
EXPANSION_BATCH_PAUSE_SECONDS = 20

# Retry policy for a single batch call, mirroring podcastindex._get(): retry
# 429s and 5xxs, don't retry anything else (bad JSON, wrong term count, a
# bad key fail the same way every time, and retrying just pays for the same
# failure three times). GROQ_TPM is a per-MINUTE ceiling, unlike Podcast
# Index's per-request one, so backoff needs to be long enough to plausibly
# clear a minute-scale window rather than podcastindex.py's 1.5s base:
# 15s/30s/60s across three attempts covers up to ~a minute of waiting for
# room in the window without padding out an unrelated 5xx blip needlessly.
GROQ_MAX_RETRIES = 3
GROQ_BACKOFF_BASE = 15.0  # seconds; 15, 30, 60


class UniverseError(RuntimeError):
    """Universe construction could not safely produce a candidate set."""


@dataclass
class Interest:
    """One topic from config.TOPICS, plus the terms used to search for it.

    `text` is the topic label (e.g. "Technology & AI"). There is no per-user
    free text in v3 — matching happens on the stored `topic` slug, not on
    this string — so `text` exists only for CLI/log readability and to keep
    expand_interests()'s existing signature. `terms` bridge the gap between
    the topic and the way podcast shows actually name themselves.
    """

    text: str
    terms: list[str]


@dataclass
class FeedHit:
    feed_id: int
    feed_url: str
    title: str
    newest_item_pubdate: int | None
    description: str = ""
    language: str = ""
    dead: int = 0
    episode_count: int = 0
    matched_terms: set[str] = field(default_factory=set)
    matched_interests: set[int] = field(default_factory=set)
    score: float = 0.0

    @property
    def days_since_episode(self) -> float | None:
        if not self.newest_item_pubdate:
            return None
        return (time.time() - self.newest_item_pubdate) / 86400


# --- topic terms (optional offline fallback) ---------------------------------


def load_topic_terms(path: Path) -> dict[str, list[str]]:
    """Optional --use-file-terms fallback: search terms keyed by topic slug.

    v1's load_interests() matched fallback terms against free user text,
    which no longer exists — v3 has no per-user interest, only
    config.TOPICS. Format:

        [[topic]]
        slug = "technology-ai"
        terms = ["term one", "term two", ...]

    Debugging convenience only. The Step 3 check's dry run and the live seed
    both call expand_interests() for real; this exists so iteration doesn't
    have to pay for a Groq call every time. Repointing a terms file at
    config.TOPICS with real per-topic terms is IMPLEMENTATION-PLAN.md Step
    10's job — until then this raises cleanly on a file with no topic-keyed
    entries, rather than silently seeding an empty or mismatched universe.
    """
    if not path.exists():
        raise SystemExit(
            f"No terms file at {path}.\n"
            f"--use-file-terms needs [[topic]] entries keyed by slug — "
            f"see load_topic_terms()'s docstring for the format."
        )
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    terms_by_slug: dict[str, list[str]] = {}
    for entry in data.get("topic", []):
        slug = (entry.get("slug") or "").strip()
        values = [t.strip() for t in entry.get("terms", []) if t.strip()]
        if slug:
            terms_by_slug[slug] = values
    return terms_by_slug


# --- expansion -----------------------------------------------------------------


def expand_interests(
    interest_texts: list[str], client: Groq | None = None
) -> list[Interest]:
    """Expand every interest in one Groq call into precise search terms.

    Podcast Index searches show titles and descriptions. Short category-like
    phrases work; broad single words and descriptive sentences pull in noisy
    result tails. The response uses input indexes so the caller's wording is
    preserved exactly rather than trusted to a model round-trip.
    """
    texts = [text.strip() for text in interest_texts if text.strip()]
    if not texts:
        raise UniverseError("At least one non-empty interest is required.")
    if not config.GROQ_API_KEY and client is None:
        raise UniverseError("GROQ_API_KEY missing from .env; Block 2 needs it for expansion.")

    numbered = "\n".join(f"[{index}] {text}" for index, text in enumerate(texts))
    short_term_minimum = max(1, config.TERMS_PER_INTEREST * 3 // 4)
    prompt = f"""Expand each listener interest into exactly {config.TERMS_PER_INTEREST}
Podcast Index show-search terms.

Podcast Index matches show titles and descriptions, not episode contents. It
has low recall for detailed phrases, so these must be broad enough to return
podcast shows while remaining inside the stated interest.
Rules:
- At least {short_term_minimum} of the {config.TERMS_PER_INTEREST} terms for each
  interest must be 1 or 2 words. Never use more than 3 words.
- Return established fields, genres, professions, or podcast categories—not
  episode topics, tutorial queries, tasks, or narrow use cases.
- Cover distinct facets of the interest with vocabulary commonly used in show
  names and descriptions.
- Do not return generic standalone words such as food, design, creative,
  productivity, automation, technology, business, AI, or eating.
- Do not return news-only terms unless the interest explicitly asks for news.
- Do not add interests the listener did not state.
- Preserve each input index.

Good catalogue terms: "food science", "home cooking", "culinary", "baking",
"AI engineering", "MLOps", "AI agents", "applied AI", "product design",
"UX", "typography", "design systems".
Bad episode-level terms: "knife skills tutorial", "AI for data entry",
"typography for UI", "prototyping with Figma", "meal prep efficiency".

Interests:
{numbered}

Return only JSON with this shape:
{{"interests":[{{"index":0,"terms":["term one","term two"]}}]}}
"""

    groq_client = client or Groq(api_key=config.GROQ_API_KEY)
    try:
        response = groq_client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You produce precise podcast catalogue search terms and valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_completion_tokens=2500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        data = json.loads(raw)
    except Exception as exc:
        raise UniverseError("Groq interest expansion failed.") from exc

    by_index: dict[int, list[str]] = {}
    for item in data.get("interests", []):
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(texts):
            continue
        terms: list[str] = []
        seen: set[str] = set()
        for value in item.get("terms", []):
            term = " ".join(str(value).strip().split())
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
        by_index[index] = terms

    expected = config.TERMS_PER_INTEREST
    invalid = [index for index in range(len(texts)) if len(by_index.get(index, [])) != expected]
    if invalid:
        raise UniverseError(
            f"Groq expansion must return exactly {expected} unique terms for every interest; "
            f"invalid indexes: {invalid}"
        )
    return [Interest(text=text, terms=by_index[index]) for index, text in enumerate(texts)]


def _expand_batch_with_retry(texts: list[str], client: Groq) -> list[Interest]:
    """One batch call, retried on rate limits and server errors only.

    expand_interests() itself never retries — correct for v1, where it made
    one call per run. A seed now makes several calls back to back, so a
    single 429 against the real GROQ_TPM ceiling would otherwise kill the
    whole run for one transient blip. Anything else (malformed JSON, a
    wrong term count, a bad key) fails the same way on every attempt, so
    those are not retried — see GROQ_MAX_RETRIES's comment for the backoff
    reasoning.
    """
    last_error: Exception | None = None
    for attempt in range(GROQ_MAX_RETRIES):
        try:
            return expand_interests(texts, client=client)
        except UniverseError as exc:
            cause = exc.__cause__
            retryable = isinstance(cause, RateLimitError) or (
                isinstance(cause, APIStatusError) and cause.status_code >= 500
            )
            if not retryable or attempt == GROQ_MAX_RETRIES - 1:
                raise
            last_error = exc
            retry_after = 0.0
            if isinstance(cause, APIStatusError):
                value = cause.response.headers.get("retry-after")
                try:
                    retry_after = float(value) if value else 0.0
                except ValueError:
                    pass
            time.sleep(max(retry_after, GROQ_BACKOFF_BASE * (2**attempt)))
    raise last_error  # pragma: no cover — loop above always returns or raises


def expand_all_topics(topic_labels: list[str], client: Groq | None = None) -> list[Interest]:
    """Batch expand_interests() across every topic in config.TOPICS.

    expand_interests() itself is unchanged — see its docstring — this only
    calls it several times and stitches the results back together.

    Each call is independently indexed 0..len(batch)-1 by expand_interests()
    (it echoes back whatever indexes it was given for that call), so a
    batch's results are mapped back to the caller's GLOBAL topic order by
    the batch's starting offset, not by re-deriving an index from content.
    An off-by-one here would silently assign one topic's terms to its
    neighbour — exactly the class of bug this repo is about — so the
    per-batch length is checked before the offset mapping is trusted.
    """
    if not topic_labels:
        raise UniverseError("At least one topic is required.")
    if not config.GROQ_API_KEY and client is None:
        raise UniverseError("GROQ_API_KEY missing from .env; Block 2 needs it for expansion.")

    groq_client = client or Groq(api_key=config.GROQ_API_KEY)
    results: list[Interest | None] = [None] * len(topic_labels)

    for start in range(0, len(topic_labels), INTERESTS_PER_EXPANSION_BATCH):
        batch = topic_labels[start : start + INTERESTS_PER_EXPANSION_BATCH]
        expanded = _expand_batch_with_retry(batch, groq_client)
        if len(expanded) != len(batch):
            raise UniverseError(
                f"expand_interests returned {len(expanded)} interests for a "
                f"{len(batch)}-topic batch starting at global index {start}."
            )
        for offset, interest in enumerate(expanded):
            results[start + offset] = interest

        if start + INTERESTS_PER_EXPANSION_BATCH < len(topic_labels):
            time.sleep(EXPANSION_BATCH_PAUSE_SECONDS)

    assert all(interest is not None for interest in results)
    return results  # type: ignore[return-value]


# --- search and merge --------------------------------------------------------


def search_all(interests: list[Interest]) -> dict[int, FeedHit]:
    """Run every term concurrently and merge results by feed_id.

    A feed that surfaces for several different terms, and ranks highly in
    each, is more central to what the user actually wants than one that
    happens to top a single search.
    """
    terms = sorted({term for interest in interests for term in interest.terms})
    hits: dict[int, FeedHit] = {}

    # Which interest each term came from, so the universe can be shared out
    # between them later. A term two interests happen to share belongs to both.
    owners: dict[str, set[int]] = {}
    for index, interest in enumerate(interests):
        for term in interest.terms:
            owners.setdefault(term, set()).add(index)

    with httpx.Client(timeout=podcastindex.TIMEOUT) as client:
        def run(term: str) -> tuple[str, list[dict]]:
            return term, podcastindex.search_shows(term, client=client)

        with ThreadPoolExecutor(max_workers=config.FEED_WORKERS) as pool:
            results = list(pool.map(run, terms))

    for term, feeds in results:
        for position, feed in enumerate(feeds):
            feed_id = feed.get("id")
            url = feed.get("url")
            if not feed_id or not url:
                continue

            hit = hits.get(feed_id)
            if hit is None:
                hit = FeedHit(
                    feed_id=feed_id,
                    feed_url=url,
                    title=(feed.get("title") or "").strip() or f"feed {feed_id}",
                    newest_item_pubdate=feed.get("newestItemPubdate"),
                    description=(feed.get("description") or "").strip(),
                    language=(feed.get("language") or "").strip(),
                    dead=feed.get("dead") or 0,
                    episode_count=feed.get("episodeCount") or 0,
                )
                hits[feed_id] = hit

            hit.matched_terms.add(term)
            hit.matched_interests |= owners.get(term, set())
            hit.score += 1.0 / (RRF_K + position + 1)

    return hits


def rank_feeds(hits: dict[int, FeedHit]) -> tuple[list[FeedHit], dict[str, int]]:
    """Drop unusable feeds, then order by fused score.

    All four filters use metadata the search response already returns, so they
    cost nothing. They exist to stop junk consuming slots in a fixed-size
    universe — not to judge relevance, which is the ranker's job in Block 5.

    Returns (kept, {reason: count}).
    """
    kept: list[FeedHit] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for hit in hits.values():
        if hit.dead:
            drop("marked dead")
            continue

        age = hit.days_since_episode
        # Unknown pubdate is kept: absent metadata is common and is not
        # evidence the show is dead. Block 4's fetch will find out.
        if age is not None and age > config.UNIVERSE_MAX_FEED_AGE_DAYS:
            drop(f"stale >{config.UNIVERSE_MAX_FEED_AGE_DAYS}d")
            continue

        # Unknown language is kept for the same reason.
        if config.UNIVERSE_LANGUAGES and hit.language:
            lang = hit.language.lower()
            if not any(lang.startswith(p) for p in config.UNIVERSE_LANGUAGES):
                drop("wrong language")
                continue

        # episodeCount of 0 usually means "not counted yet", not "empty".
        if 0 < hit.episode_count < config.UNIVERSE_MIN_EPISODES:
            drop(f"under {config.UNIVERSE_MIN_EPISODES} episodes")
            continue

        kept.append(hit)

    kept.sort(key=lambda h: (h.score, len(h.matched_terms)), reverse=True)

    # Podcast Index occasionally carries the same feed under multiple feed IDs.
    # Keep the highest-ranked spelling/version so duplicates do not consume the
    # fixed 200 slots.
    unique: list[FeedHit] = []
    seen_titles: set[str] = set()
    blocked = tuple(value.casefold() for value in config.UNIVERSE_TITLE_BLOCKLIST)
    for hit in kept:
        title_key = re.sub(r"[^a-z0-9]+", " ", hit.title.casefold()).strip()
        if any(value in title_key for value in blocked):
            drop("blocked network")
            continue
        if title_key and title_key in seen_titles:
            drop("duplicate title")
            continue
        seen_titles.add(title_key)
        unique.append(hit)

    return unique, dropped


def allocate(ranked: list[FeedHit], interest_count: int, target: int) -> list[FeedHit]:
    """Share the universe out between interests instead of pooling them.

    Search recall varies enormously by subject: "AI engineering" returns 40
    shows, "molecular gastronomy" returns none. Ranked as one pile, the loudest
    interest simply wins — a three-interest profile came back 55 AI shows to 11
    cooking, so one stated interest was effectively absent from the product.

    A round-robin draft fixes that. Each interest takes its own highest-scoring
    unclaimed feed in turn, so shares are equal, and an interest that runs out
    early hands its remaining slots to the others rather than leaving holes.
    """
    if interest_count <= 1:
        return ranked[:target]

    queues = [
        [hit for hit in ranked if index in hit.matched_interests]
        for index in range(interest_count)
    ]
    cursors = [0] * interest_count
    picked: list[FeedHit] = []
    taken: set[int] = set()

    while len(picked) < target:
        progress = False
        for index in range(interest_count):
            if len(picked) >= target:
                break
            queue, cursor = queues[index], cursors[index]
            while cursor < len(queue) and queue[cursor].feed_id in taken:
                cursor += 1
            cursors[index] = cursor
            if cursor < len(queue):
                picked.append(queue[cursor])
                taken.add(queue[cursor].feed_id)
                cursors[index] = cursor + 1
                progress = True
        if not progress:
            break
    return picked


# --- persistence -------------------------------------------------------------


def save_shows(conn, feeds: list[FeedHit]) -> int:
    """Replace the shared show universe. No user_id — one universe, not N.

    Keeps the legacy seed's safety behaviour without its former user scope:
    the upsert never touches `status`, so a
    retained muted show stays muted (S3-12); rows absent from a successful
    rebuild are deleted, so re-seeding can't grow the universe past its
    target; and an empty result raises instead of erasing a good universe
    (S3-11).

    Also writes show_topic from FeedHit.matched_interests (S3-06).
    show_topic is coverage/debug data, never used for matching at send time
    (ARCHITECTURE section 5), so a retained show's topic rows are rebuilt
    from this run's matches rather than accumulated across seeds — a show
    that stops matching a topic should stop showing that topic's coverage.
    """
    if not feeds:
        raise UniverseError("Refusing to replace the show universe with an empty list.")

    conn.executemany(
        """
        INSERT INTO show (feed_id, feed_url, title)
        VALUES (?, ?, ?)
        ON CONFLICT (feed_id) DO UPDATE SET
            feed_url = excluded.feed_url,
            title    = excluded.title
        """,
        [(f.feed_id, f.feed_url, f.title) for f in feeds],
    )

    feed_ids = [f.feed_id for f in feeds]
    placeholders = ",".join("?" for _ in feed_ids)
    conn.execute(f"DELETE FROM show WHERE feed_id NOT IN ({placeholders})", feed_ids)

    show_id_by_feed = {
        row["feed_id"]: row["id"]
        for row in conn.execute(
            f"SELECT id, feed_id FROM show WHERE feed_id IN ({placeholders})", feed_ids
        ).fetchall()
    }

    show_ids = list(show_id_by_feed.values())
    id_placeholders = ",".join("?" for _ in show_ids)
    conn.execute(f"DELETE FROM show_topic WHERE show_id IN ({id_placeholders})", show_ids)

    show_topic_rows = [
        (show_id_by_feed[f.feed_id], config.TOPIC_SLUGS[index])
        for f in feeds
        for index in sorted(f.matched_interests)
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO show_topic (show_id, topic) VALUES (?, ?)",
        show_topic_rows,
    )
    return len(feeds)


def seed_global(
    target: int,
    use_file_terms: bool = False,
    terms_path: Path | None = None,
    groq_client: Groq | None = None,
) -> tuple[list[Interest], list[FeedHit], list[FeedHit], dict[str, int]]:
    """Expand config.TOPICS, search, rank and allocate — everything short of
    persistence. Returns (interests, ranked, kept, dropped) so the CLI (and
    tests) can inspect or export the exact same result whether or not it
    goes on to write to the database.
    """
    if use_file_terms:
        term_map = load_topic_terms(terms_path or config.PROJECT_ROOT / "interests.toml")
        missing = [slug for slug, _ in config.TOPICS if not term_map.get(slug)]
        if missing:
            raise UniverseError(f"No fallback terms for: {', '.join(missing)}")
        interests = [Interest(text=label, terms=term_map[slug]) for slug, label in config.TOPICS]
    else:
        interests = expand_all_topics(list(config.TOPIC_LABELS), client=groq_client)

    hits = search_all(interests)
    ranked, dropped = rank_feeds(hits)
    keep = allocate(ranked, len(interests), target)
    return interests, ranked, keep, dropped


def export_csv(path: Path, kept: list[FeedHit]) -> int:
    """Write the resulting universe to CSV: feed_id, title, feed_url, topics.

    `topics` is the matched topic slugs, comma-joined — the same signal
    show_topic stores, in a form readable without a database. Works
    identically under --dry-run, so a list can be reviewed before anything
    is written.
    """
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["feed_id", "title", "feed_url", "topics"])
        for hit in kept:
            slugs = [config.TOPIC_SLUGS[index] for index in sorted(hit.matched_interests)]
            writer.writerow([hit.feed_id, hit.title, hit.feed_url, ",".join(slugs)])
    return len(kept)


def review_sample(
    kept: list[FeedHit], topic_count: int = 5, per_topic: int = 10
) -> list[tuple[int, FeedHit]]:
    """Choose the Step 3 manual gate: 50 titles spread across five topics.

    Sampling the first 50 allocated rows can accidentally show only the
    topics with the deepest queues. Pick evenly spaced topic indexes and
    take each topic's first ten allocated feeds instead, deduplicating a
    cross-topic show so the reviewer sees 50 distinct titles when coverage
    permits it.
    """
    if topic_count <= 0 or per_topic <= 0:
        return []

    last_index = len(config.TOPICS) - 1
    if topic_count == 1:
        topic_indexes = [0]
    else:
        topic_indexes = sorted(
            {round(position * last_index / (topic_count - 1)) for position in range(topic_count)}
        )

    sample: list[tuple[int, FeedHit]] = []
    seen: set[int] = set()
    for topic_index in topic_indexes:
        added = 0
        for hit in kept:
            if topic_index not in hit.matched_interests or hit.feed_id in seen:
                continue
            sample.append((topic_index, hit))
            seen.add(hit.feed_id)
            added += 1
            if added == per_topic:
                break
    return sample


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--seed-global",
        action="store_true",
        help="search, rank and persist the shared show universe",
    )
    parser.add_argument(
        "--target", type=int, default=config.SHOW_TARGET, help="how many shows to keep"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print (and optionally export) without writing to the database",
    )
    parser.add_argument(
        "--use-file-terms",
        action="store_true",
        help="use terms from interests.toml instead of calling Groq (debugging only)",
    )
    parser.add_argument(
        "--export",
        type=Path,
        help="write the resulting universe to CSV (feed_id, title, feed_url, topics)",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.seed_global:
        parser.error("--seed-global is required unless --dry-run is set")

    if not args.use_file_terms:
        batches = -(-len(config.TOPIC_LABELS) // INTERESTS_PER_EXPANSION_BATCH)
        print(
            f"expanding {len(config.TOPIC_LABELS)} topics with Groq "
            f"({INTERESTS_PER_EXPANSION_BATCH}/batch, {batches} batches)...",
            flush=True,
        )

    started = time.time()
    interests, ranked, keep, dropped = seed_global(
        target=args.target, use_file_terms=args.use_file_terms
    )
    elapsed = time.time() - started

    term_count = len({t for i in interests for t in i.terms})
    print(f"{len(interests)} topics, {term_count} unique search terms")
    for interest in interests:
        print(f"  {interest.text}: {', '.join(interest.terms)}")
    print()

    print(f"{len(ranked) + sum(dropped.values())} unique feeds found  [{elapsed:.1f}s]")
    for reason, count in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  -{count:>5}  {reason}")
    print(f"  ={len(ranked):>5}  usable, keeping {len(keep)}")
    for index, interest in enumerate(interests):
        share = sum(1 for hit in keep if index in hit.matched_interests)
        print(f"    {share:>4}  {interest.text[:60]}")
    print()

    # THE CHECK. Read these 50 names across five topics. This list is the
    # ceiling on everything the product will ever recommend to subscribers.
    print("manual review sample (up to 10 shows across each of 5 topics):")
    for position, (topic_index, hit) in enumerate(review_sample(keep), 1):
        age = hit.days_since_episode
        age_text = f"{age:>4.0f}d" if age is not None else "   ?"
        slugs = ",".join(config.TOPIC_SLUGS[index] for index in sorted(hit.matched_interests))
        label = config.TOPIC_LABELS[topic_index]
        print(
            f"{position:>3}. {label[:20]:<20} {hit.title[:42]:<42} "
            f"{age_text}  x{len(hit.matched_terms)}  {slugs}"
        )

    if len(keep) < args.target:
        print(
            f"\nwarn  only {len(keep)} of {args.target} shows. Add more search terms "
            f"per topic, or broaden them slightly, and rerun."
        )

    if args.export:
        written = export_csv(args.export, keep)
        print(f"\nwrote {written} rows to {args.export}")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    with db.session() as conn:
        saved = save_shows(conn, keep)
    print(f"\nwrote {saved} show rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
