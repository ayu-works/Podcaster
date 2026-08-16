"""Schema and connection (ARCHITECTURE section 5).

Nine-table schema: everything AI-derived lives on `episode`, computed once
by block-5-tag and read by everyone at send time. `show` has no `user_id` —
that single absence is the difference between v1 and v2.

Invariants that live here rather than in application code:

1. `episode.guid` is UNIQUE. Dedupe on guid, never on title — feeds
   republish episodes with edited titles constantly.
2. `fetch_cutoff_at`, not `finished_at`, is the clock (invariant 2). It is
   stamped at the *start* of a run, before fetch polls anything, so
   `last_good_cutoff()` below reads that column and no other.
3. `sent` records attempts, not successes (invariant 3): a row is written
   `status='pending'` and committed before Resend is ever called.
4. `tagged_at IS NULL` is the tagging work queue, bounded by `tag_attempts`
   (invariant 4), so a description that can never produce a grounded reason
   is retried a bounded number of times, not forever.
"""

import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriber (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    unsub_token   TEXT NOT NULL UNIQUE,     -- must be unguessable; see block-3
    confirm_token TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed_at  TEXT,                     -- NULL until double opt-in completes
    -- pending until /confirm/<token>; only 'active' ever receives a digest
    -- (ARCHITECTURE section 7, "Anyone can subscribe anyone").
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'active', 'paused', 'unsubscribed'))
);

CREATE TABLE IF NOT EXISTS subscription (
    subscriber_id INTEGER NOT NULL REFERENCES subscriber(id) ON DELETE CASCADE,
    topic         TEXT NOT NULL,            -- slug from config.TOPIC_SLUGS
    PRIMARY KEY (subscriber_id, topic)
);

-- the global universe. No user_id — the difference between v1 and v2.
CREATE TABLE IF NOT EXISTS show (
    id         INTEGER PRIMARY KEY,
    feed_id    INTEGER NOT NULL UNIQUE,
    feed_url   TEXT NOT NULL,
    title      TEXT NOT NULL,
    added_at   TEXT NOT NULL DEFAULT (datetime('now')),
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'muted'))
);

-- which topic's search terms surfaced this show. Debug/coverage only,
-- never used for matching at send time (ARCHITECTURE section 5).
CREATE TABLE IF NOT EXISTS show_topic (
    show_id INTEGER NOT NULL REFERENCES show(id) ON DELETE CASCADE,
    topic   TEXT NOT NULL,
    PRIMARY KEY (show_id, topic)
);

-- one shared pool, tagged once and read by every subscriber (section 2,
-- "constraint two"). tagged_at NULL means not yet tagged; tag_attempts
-- bounds the retry queue so a bad description isn't retried forever.
CREATE TABLE IF NOT EXISTS episode (
    id           INTEGER PRIMARY KEY,
    guid         TEXT NOT NULL UNIQUE,
    feed_id      INTEGER NOT NULL,
    show_name    TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    duration_sec INTEGER,
    published_at TEXT,
    web_url      TEXT,
    score        INTEGER,
    why          TEXT,
    tagged_at    TEXT,                      -- NULL = not yet tagged (invariant 4)
    tag_attempts INTEGER NOT NULL DEFAULT 0, -- capped at config.TAG_MAX_ATTEMPTS
    tag_error    TEXT                       -- last failure reason, for diagnosis
);
CREATE INDEX IF NOT EXISTS idx_episode_feed_pub ON episode(feed_id, published_at);

-- The tagging work queue is `tagged_at IS NULL AND tag_attempts < N`
-- (invariant 4) and curation selects `tagged_at > :previous_cutoff`
-- (invariant 5, stage 3) — both scan only untagged rows, which a partial
-- index keeps small even as the tagged pool grows into the tens of
-- thousands. This is also S2-01b's check that the dialect ports unchanged.
CREATE INDEX IF NOT EXISTS idx_episode_untagged ON episode(tagged_at)
    WHERE tagged_at IS NULL;

-- the stored match (section 2, "constraint two"): a row here is what makes
-- sending a pure SQL join instead of a per-user model call.
CREATE TABLE IF NOT EXISTS episode_topic (
    episode_id INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    topic      TEXT NOT NULL,
    PRIMARY KEY (episode_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_episode_topic_topic ON episode_topic(topic);

CREATE TABLE IF NOT EXISTS run (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- stamped at the *start* of fetch, before anything is polled -- this,
    -- not finished_at, is the clock. See last_good_cutoff() (invariant 2).
    fetch_cutoff_at TEXT,
    finished_at     TEXT,
    fetched         INTEGER,
    tagged          INTEGER,
    emails_sent     INTEGER,
    emails_failed   INTEGER,
    -- stages 1-3 are all-or-nothing (failed); stage 4 is per-recipient, so
    -- a run that fetched/tagged/curated fine but had some sends fail is
    -- 'partial', not 'failed' -- its window still doesn't need re-covering.
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'ok', 'partial', 'failed'))
);

-- the day's editorial output, ~200 rows: PICKS_PER_TOPIC per topic per run.
CREATE TABLE IF NOT EXISTS daily_pick (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    topic      TEXT NOT NULL,
    episode_id INTEGER NOT NULL REFERENCES episode(id),
    rank       INTEGER NOT NULL,
    -- curate.py must be safe to re-run against the same run_id -- Step 9's
    -- --skip-fetch/--skip-tag exist specifically so later stages can be
    -- re-run against existing data. Without this, a second curate pass
    -- silently doubles a day's picks, and Step 6's "every topic <= 10
    -- rows" check then fails looking like a curation-logic bug rather
    -- than a re-run artefact.
    UNIQUE (run_id, topic, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_pick_run_topic ON daily_pick(run_id, topic);

-- delivery attempts, not just successes (invariant 3). A row is written
-- pending and committed *before* Resend is called, so a crash between the
-- commit and the API call is recoverable rather than ambiguous.
CREATE TABLE IF NOT EXISTS sent (
    subscriber_id INTEGER NOT NULL REFERENCES subscriber(id) ON DELETE CASCADE,
    episode_id    INTEGER NOT NULL REFERENCES episode(id),
    run_id        INTEGER REFERENCES run(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at       TEXT,
    -- the only re-send guard, and it is per subscriber (invariant 6) --
    -- one person having seen an episode must not hide it from anyone else.
    PRIMARY KEY (subscriber_id, episode_id)
);
"""

TABLES = (
    "subscriber",
    "subscription",
    "show",
    "show_topic",
    "episode",
    "episode_topic",
    "run",
    "daily_pick",
    "sent",
)


def connect(url=None, token=None):
    """Open a connection: Turso when `url` (or config.DATABASE_URL) is a
    remote `libsql://` or `https://` address, a local SQLite file otherwise.

    The local branch is what every check in this step actually runs
    against -- there are no Turso credentials yet (deliberate deferral, not
    an oversight). Unset/empty DATABASE_URL falls back to config.DB_PATH;
    an explicit `file:...` URL (e.g. `DATABASE_URL=file:test.db`) points at
    a throwaway file so tests need no network and no shared remote state
    (S2-01c).

    `libsql` is imported lazily, inside the remote branch only. A
    module-level `import libsql` would make this file -- and therefore
    every block, since all of them import config/db -- unimportable on a
    machine that doesn't have the package installed, which is this one.
    """
    url = config.DATABASE_URL if url is None else url

    if isinstance(url, str) and (
        url.startswith("libsql://") or url.startswith("https://")
    ):
        import libsql  # noqa: local import, see docstring

        conn = libsql.connect(url, auth_token=token or config.DATABASE_TOKEN)
        # Round-trips immediately, so a bad token raises here -- at connect
        # time -- rather than silently at whatever query happens to run
        # first (S2-00). Never wrap this in try/except: the whole point is
        # that a bad credential is loud.
        conn.execute("PRAGMA foreign_keys = ON")

        # Whether libsql.Connection supports dict-style row access the same
        # way sqlite3.Connection does was unverified at build time -- there
        # are no Turso credentials to test against yet. table_names() and
        # last_good_cutoff() below both read rows by column name, and so
        # does every query in blocks 2-7 -- "every query ports unchanged"
        # is the entire stated reason for choosing Turso over Postgres
        # (ARCHITECTURE section 7). So prove it here, at connect time,
        # rather than let it surface as a silent tuple-index crash the day
        # credentials arrive. Do not fall back to index access on failure
        # and do not swallow the error -- an unverifiable driver behaviour
        # must fail loudly, with the reason named, on the first connection.
        try:
            conn.row_factory = sqlite3.Row
            probe = conn.execute("SELECT 1 AS one").fetchone()
            probe_ok = probe["one"] == 1
        except Exception as exc:
            raise RuntimeError(
                "libsql does not support named column access (row['col']) "
                "the way sqlite3.Row does. table_names(), last_good_cutoff(), "
                "and every query in blocks 2-7 depend on it -- this must be "
                "fixed in db.connect() before anything else is built on Turso."
            ) from exc
        if not probe_ok:
            raise RuntimeError(
                "libsql row_factory was accepted but named column access "
                "('SELECT 1 AS one' via row['one']) did not return the "
                "expected value. table_names(), last_good_cutoff(), and "
                "every query in blocks 2-7 depend on it -- this must be "
                "fixed in db.connect() before anything else is built on Turso."
            )

        return conn

    # Local SQLite fallback. WAL is a local-file concern and dropped for
    # the Turso branch above, but it's still correct here.
    path = (
        url[len("file:") :]
        if isinstance(url, str) and url.startswith("file:")
        else (url or config.DB_PATH)
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(path=None):
    """Connection that commits on success and rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path=None) -> None:
    """Create the schema. Safe to run repeatedly."""
    with session(path) as conn:
        conn.executescript(SCHEMA)


def table_names(path=None) -> list[str]:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


def last_good_cutoff(conn):
    """The clock: the start of the window the next run should fetch from.

    Reads `fetch_cutoff_at`, stamped before fetch polls anything, never
    `finished_at`, stamped 20-30 minutes later after tagging finishes.
    Using finished_at would leave every episode published during that
    window permanently unfetched by any run (invariant 2) -- overlap from
    reading the earlier timestamp is safe and absorbed by the guid upsert;
    a gap from reading the later one is not recoverable.

    'partial' counts: fetch, tag and curate all succeeded for that run,
    only some deliveries failed, and those retry through `sent`, not by
    rewinding the pipeline. 'running' and 'failed' never advance the clock.

    Returns None on a virgin database or when no run has ever reached
    ok/partial; the caller floors the lookback at config.MAX_LOOKBACK_DAYS.
    """
    row = conn.execute(
        "SELECT MAX(fetch_cutoff_at) AS cutoff FROM run WHERE status IN ('ok', 'partial')"
    ).fetchone()
    return row["cutoff"] if row else None


if __name__ == "__main__":
    init_db()
    # Never config.DB_PATH unconditionally: DATABASE_URL can point init_db()
    # at Turso or a throwaway local file (S2-01c), and the printed location
    # must say where the schema actually landed. Never print DATABASE_TOKEN.
    print(f"initialised {config.DATABASE_URL or config.DB_PATH}")
    print("tables:", ", ".join(table_names()))
