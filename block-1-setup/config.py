"""All tunable numbers in one place (ARCHITECTURE section 9).

Nothing here should be duplicated into logic. When a run feels wrong, this is
the first file to open — RELEVANCE_BAR alone decides whether the digest feels
curated or spammy.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Blocks live in sibling folders (block-1-setup, block-2-universe, ...).
# The env file, the database and the logs are shared at the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# --- Topics -------------------------------------------------------------------

# The single source for show seeding, the tagging prompt's allowed slugs,
# curation, and the onboarding checkboxes (ARCHITECTURE section 9). Do not
# copy this list anywhere else — four copies is four chances to drift. Order
# matches the allowed-slug list hardcoded into the tagging prompt in
# ARCHITECTURE section 6, stage 2.
TOPICS = (
    ("technology-ai", "Technology & AI"),
    ("business-startups", "Business & Startups"),
    ("design", "Design"),
    ("science", "Science"),
    ("history", "History"),
    ("finance", "Finance"),
    ("culture", "Culture"),
    ("politics", "Politics"),
    ("health-fitness", "Health & Fitness"),
    ("comedy", "Comedy"),
    ("true-crime", "True Crime"),
    ("sport", "Sport"),
    ("personal-development", "Personal Development"),
    ("food-cooking", "Food & Cooking"),
    ("music", "Music"),
    ("film-tv", "Film & TV"),
    ("books-writing", "Books & Writing"),
    ("philosophy", "Philosophy"),
    ("climate-energy", "Climate & Energy"),
    ("travel", "Travel"),
)
TOPIC_SLUGS = tuple(slug for slug, _ in TOPICS)
TOPIC_LABELS = tuple(label for _, label in TOPICS)

# --- Tuning -----------------------------------------------------------------

RELEVANCE_BAR = 70  # the most important number here
PICKS_PER_TOPIC = 10  # rows in daily_pick per topic per run
MAX_PER_EMAIL = 10  # cap after merging a subscriber's topics

# Caps a show across the whole email, at send time. CURATE_MAX_PER_SHOW below
# only caps a show per topic list — without this second cap, a subscriber to
# four topics could receive eight episodes from the same show.
MAX_PER_SHOW_PER_EMAIL = 2

# Caps a show per topic list, at curation time. Enforced in code, not the
# tagging prompt — a prompt instruction is a request, a post-filter is a
# guarantee.
CURATE_MAX_PER_SHOW = 2

# Staleness floor for late-tagged episodes: a long tagging backlog must not
# surface three-week-old episodes as today's picks.
CURATE_MAX_AGE_DAYS = 7

# Live testing at v1's scale showed fewer terms could not leave 200 fresh
# feeds for a three-interest profile after staleness and usability filters
# (block-2-universe/README.md). Not re-verified at 20 topics / SHOW_TARGET
# scale — Step 10 calls for re-measuring there.
TERMS_PER_INTEREST = 18

TAG_BATCH_SIZE = 20  # episodes per tagging call
TAG_MAX_TOPICS = 3  # topics one episode may carry

# Then abandon; without a cap, an episode whose description reliably produces
# a generic why-line is retried on every run, forever, at cost, while looking
# like a transient backlog rather than a permanent one.
TAG_MAX_ATTEMPTS = 3

MIN_DESC_CHARS = 100
DESC_TRUNCATE = 400
MAX_LOOKBACK_DAYS = 5
FEED_WORKERS = 15

# Feeds with nothing published in this many days never enter the universe.
UNIVERSE_MAX_FEED_AGE_DAYS = 60

# Language prefixes to keep ("en" matches en, en-us, en-GB...). Empty tuple
# disables the filter.
UNIVERSE_LANGUAGES = ("en",)

# Feeds with fewer episodes than this are abandoned or trailer-only.
UNIVERSE_MIN_EPISODES = 5

# Keep the relevant head of each search result; deeper pages add mostly noise.
SEARCH_RESULTS_PER_TERM = 40

# Known catalogue-spam networks. Match is case-insensitive against show title.
# Keep this short: search terms, not a large blocklist, are the main quality
# control. These two networks repeatedly occupied multiple top-200 slots.
UNIVERSE_TITLE_BLOCKLIST = ("fexingo", "the automated daily")

# Trailers and stings are never worth ranking.
MIN_EPISODE_SEC = 180

# Ceiling per feed per fetch. A five-day window rarely holds more than a
# handful, even for daily shows; this only bounds a pathological feed.
EPISODES_PER_FEED = 25

# Groq free-tier tokens-per-minute for GROQ_MODEL. Paces tagging: a
# TAG_BATCH_SIZE batch is ~4,400 tokens all-in, so this ceiling sets tagging
# calls to roughly 2 a minute. Check the real number for your tier with
# `x-ratelimit-limit-tokens` on any response — raising it speeds up a run,
# with no code change.
GROQ_TPM = 8_000

# Groq free-tier tokens-per-day for GROQ_MODEL. This, not GROQ_TPM, is the
# binding constraint on the whole product's reach — see ARCHITECTURE section
# 9. It sets SHOW_TARGET below: after reserving headroom for retries and the
# monthly seed, ~170,000 usable tokens/day at ~220 tokens/episode caps tagging
# at ~770 episodes per run.
GROQ_TPD = 200_000

# DERIVED from GROQ_TPD, not chosen freely — see ARCHITECTURE section 9
# before raising this. Shows publish roughly every 3 to 4 days, so a 2-day
# run sees new episodes from about a third of the universe; ~770 taggable
# episodes/run x 3 is ~2,300, hence 2,500 and not 5,000. Raising SHOW_TARGET
# without raising the Groq tier silently truncates coverage, because episodes
# still get fetched but never get tagged.
SHOW_TARGET = 2_500

# --- Paths ------------------------------------------------------------------

DB_PATH = PROJECT_ROOT / "podcaster.db"
LOG_DIR = PROJECT_ROOT / "logs"
RANK_LOG_PATH = LOG_DIR / "rank.log"  # every prompt and response, Block 5
RUN_LOG_PATH = LOG_DIR / "runs.jsonl"  # one line per run, ARCHITECTURE s10

# --- Secrets ----------------------------------------------------------------

PODCASTINDEX_KEY = os.getenv("PODCASTINDEX_KEY", "")
PODCASTINDEX_SECRET = os.getenv("PODCASTINDEX_SECRET", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

FROM_EMAIL = os.getenv("FROM_EMAIL", "onboarding@resend.dev")

# --- The ranker's model -----------------------------------------------------

# Groq is the sole LLM provider for search-term expansion and ranking.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# --- Database (Turso / libSQL, ARCHITECTURE section 7) ----------------------

# Remote (libsql:// or https://) once a Turso database exists; empty for now,
# which makes db.connect() fall back to a local SQLite file at DB_PATH. A
# file:... URL points at a throwaway file instead, for tests (S2-01c).
DATABASE_URL = os.getenv("DATABASE_URL", "")
DATABASE_TOKEN = os.getenv("DATABASE_TOKEN", "")

# Which secrets each block needs before it can run. Keeps the check honest
# about what is actually blocking, rather than demanding everything up front.
KEYS_BY_BLOCK = {
    2: ("PODCASTINDEX_KEY", "PODCASTINDEX_SECRET", "GROQ_API_KEY"),
    5: ("GROQ_API_KEY",),
    6: ("RESEND_API_KEY",),
}

REQUIRED_KEYS = tuple(name for names in KEYS_BY_BLOCK.values() for name in names)


def missing_keys(block: int | None = None) -> list[str]:
    """Required secrets that are unset or still placeholders.

    Pass a block number to check only what that block needs.
    """
    names = KEYS_BY_BLOCK[block] if block else REQUIRED_KEYS
    return [n for n in names if not (v := globals().get(n, "")) or v.startswith("your_")]
