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

# --- Tuning -----------------------------------------------------------------

RELEVANCE_BAR = 70  # the most important number here
PICKS_PER_EMAIL = 2
UNIVERSE_TARGET = 200
# Three-interest onboarding profiles need 18 focused terms to leave 200 fresh
# shows after staleness and usability filters. More interests naturally create
# overlap, so dedupe and the fixed universe cap still bound the result.
TERMS_PER_INTEREST = 18
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

# Groq free-tier tokens-per-minute for GROQ_MODEL. This is the binding
# constraint on the ranker. A full pool of candidates is ~25k tokens, so the
# ranker cuts the pool down to what one call can hold. Check the real number
# for your tier with `x-ratelimit-limit-tokens` on any response — raising it
# widens the pool automatically, with no code change.
GROQ_TPM = 8000

# Upper bound on the ranking prompt. The real limit is usually GROQ_TPM minus
# the reply; this only matters on a tier generous enough that context, not
# rate, becomes the constraint.
RANK_PROMPT_TOKENS = 20000

# Episodes from any one show the ranker may see. The pool is cut to fit the
# budget, so letting a daily show spend ten slots costs nine other shows a
# hearing.
RANK_MAX_PER_SHOW = 2

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
