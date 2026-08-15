"""Build the ~200-show candidate list for a user.

Podcast Index searches show names, not episode contents, so the expensive
matching happens once — here, at the show level, where the API is strong.
Every run afterwards just polls this short list (ARCHITECTURE section 2).

**This list is the ceiling on everything the product can ever recommend.**
If it goes wrong, discovery is permanently capped and nothing downstream
will tell you. Read it by hand before trusting it — that is the Block 2
check, and it is the highest-value ten minutes in the build.

One Groq call expands all of the user's verbatim interests into catalogue-style
search terms. The text remains untouched for the ranker; only the bridge to the
way podcast shows name themselves is generated.
"""

import argparse
import json
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from groq import Groq

import podcastindex
from _shared import config, db

# Reciprocal-rank fusion constant. Merges the per-term ranked lists into one
# ordering without needing a comparable relevance score across searches; k
# damps the pull of any single term's top hit. 60 is the conventional value.
RRF_K = 60


class UniverseError(RuntimeError):
    """Universe construction could not safely produce a candidate set."""


@dataclass
class Interest:
    """One interest, verbatim, plus the terms used to search for it.

    `text` goes into the ranking prompt word for word — it is the actual
    signal. `terms` exist only to bridge the gap between how the user talks
    and what shows call themselves.
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


# --- interests ---------------------------------------------------------------


def load_interests(path: Path) -> list[Interest]:
    """Read interest text and optional fallback terms from TOML."""
    if not path.exists():
        raise SystemExit(
            f"No interests file at {path}.\n"
            f"Copy {path.parent / 'interests.example.toml'} to {path.name} and edit it."
        )
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    interests = []
    for entry in data.get("interest", []):
        text = (entry.get("text") or "").strip()
        terms = [t.strip() for t in entry.get("terms", []) if t.strip()]
        if not text:
            raise SystemExit("Every [[interest]] needs a non-empty `text`.")
        interests.append(Interest(text=text, terms=terms))

    if not interests:
        raise SystemExit("interests.toml has no [[interest]] entries.")
    return interests


def expand_interests(
    interest_texts: list[str], client: Groq | None = None
) -> list[Interest]:
    """Expand every interest in one Groq call into precise search terms.

    Podcast Index searches show titles and descriptions. Short category-like
    phrases work; broad single words and descriptive sentences pull in noisy
    result tails. The response uses input indexes so the user's wording is
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


def ensure_user(conn, email: str) -> int:
    row = conn.execute("SELECT id FROM user WHERE email = ?", (email,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO user (email) VALUES (?)", (email,))
    return cur.lastrowid


def save_interests(conn, user_id: int, interests: list[Interest]) -> None:
    """Replace the user's interest rows. `text` is what rank.py will read."""
    conn.execute("DELETE FROM interest WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO interest (user_id, text) VALUES (?, ?)",
        [(user_id, interest.text) for interest in interests],
    )


def save_candidate_shows(conn, user_id: int, feeds: list[FeedHit]) -> int:
    """Replace a user's universe while preserving retained show statuses.

    Upserts do not touch `status`, so a retained muted show stays muted. Rows
    absent from the successful rebuild are removed so the universe cannot grow
    beyond its target. An empty result raises instead of erasing a good list.
    """
    if not feeds:
        raise UniverseError("Refusing to replace a candidate universe with an empty list.")

    conn.executemany(
        """
        INSERT INTO candidate_show (user_id, feed_id, feed_url, show_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id, feed_id) DO UPDATE SET
            feed_url  = excluded.feed_url,
            show_name = excluded.show_name
        """,
        [(user_id, f.feed_id, f.feed_url, f.title) for f in feeds],
    )
    placeholders = ",".join("?" for _ in feeds)
    conn.execute(
        f"DELETE FROM candidate_show WHERE user_id = ? "
        f"AND feed_id NOT IN ({placeholders})",
        [user_id, *(feed.feed_id for feed in feeds)],
    )
    return len(feeds)


def build(
    conn,
    user_id: int,
    interests: list[Interest] | list[str],
    target: int | None = None,
    groq_client: Groq | None = None,
) -> list[FeedHit]:
    """Search, rank, and persist the candidate universe for one user.

    Called by app.py's /subscribe (Block 3) and by the CLI below.
    """
    texts = [item.text if isinstance(item, Interest) else item for item in interests]
    expanded = expand_interests(texts, client=groq_client)
    hits = search_all(expanded)
    ranked, _ = rank_feeds(hits)
    keep = allocate(ranked, len(expanded), target or config.UNIVERSE_TARGET)
    save_candidate_shows(conn, user_id, keep)
    return keep


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--interests",
        type=Path,
        default=config.PROJECT_ROOT / "interests.toml",
        help="TOML file of interests and search terms",
    )
    parser.add_argument("--email", help="user to build for; created if new")
    parser.add_argument(
        "--target", type=int, default=config.UNIVERSE_TARGET, help="how many shows to keep"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the list without writing to the database",
    )
    parser.add_argument(
        "--use-file-terms",
        action="store_true",
        help="use optional TOML search terms instead of generating new ones",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.email:
        parser.error("--email is required unless --dry-run is set")

    loaded = load_interests(args.interests)
    if args.use_file_terms:
        missing = [interest.text for interest in loaded if not interest.terms]
        if missing:
            raise UniverseError(f"No fallback terms for: {', '.join(missing)}")
        interests = loaded
    else:
        print("expanding interests with Groq...", flush=True)
        interests = expand_interests([interest.text for interest in loaded])
    term_count = len({t for i in interests for t in i.terms})
    print(f"{len(interests)} interests, {term_count} unique search terms")
    for interest in interests:
        print(f"  {interest.text}: {', '.join(interest.terms)}")
    print("searching...", flush=True)

    started = time.time()
    hits = search_all(interests)
    ranked, dropped = rank_feeds(hits)
    keep = allocate(ranked, len(interests), args.target)
    elapsed = time.time() - started

    print(f"\n{len(hits)} unique feeds found  [{elapsed:.1f}s]")
    for reason, count in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  -{count:>5}  {reason}")
    print(f"  ={len(ranked):>5}  usable, keeping {len(keep)}")
    for index, interest in enumerate(interests):
        share = sum(1 for hit in keep if index in hit.matched_interests)
        print(f"    {share:>4}  {interest.text[:60]}")
    print()

    # THE CHECK. Read these names. This list is the ceiling on everything
    # the product will ever recommend to you.
    for position, hit in enumerate(keep, 1):
        age = hit.days_since_episode
        age_text = f"{age:>4.0f}d" if age is not None else "   ?"
        print(f"{position:>3}. {hit.title[:58]:<58} {age_text}  x{len(hit.matched_terms)}")

    if len(keep) < args.target:
        print(
            f"\nwarn  only {len(keep)} of {args.target} shows. Add more search terms "
            f"per interest, or broaden them slightly, and rerun."
        )

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    with db.session() as conn:
        user_id = ensure_user(conn, args.email)
        save_interests(conn, user_id, interests)
        saved = save_candidate_shows(conn, user_id, keep)
    print(f"\nwrote {saved} candidate_show rows for {args.email} (user {user_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
