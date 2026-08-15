"""Schema and connection (ARCHITECTURE section 5).

Two invariants live here rather than in application code:

1. `episode.guid` is UNIQUE. Dedupe on guid, never on title — feeds republish
   episodes with edited titles constantly.
2. `digest_item` is the record of what a user has already been sent. fetch.py
   filters against it before the ranker ever sees a candidate; the LLM is never
   responsible for remembering.
"""

import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_run_at TEXT,
    status      TEXT NOT NULL DEFAULT 'active'   -- active | paused
);

CREATE TABLE IF NOT EXISTS interest (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    text    TEXT NOT NULL                        -- "AI agents in production"
);
CREATE INDEX IF NOT EXISTS idx_interest_user ON interest(user_id);

-- the 200
CREATE TABLE IF NOT EXISTS candidate_show (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    feed_id    INTEGER NOT NULL,
    feed_url   TEXT NOT NULL,
    show_name  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',   -- active | muted
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, feed_id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_show_user ON candidate_show(user_id, status);

-- cache, shared across users
CREATE TABLE IF NOT EXISTS episode (
    id           INTEGER PRIMARY KEY,
    guid         TEXT NOT NULL UNIQUE,
    feed_id      INTEGER NOT NULL,
    show_name    TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    duration_sec INTEGER,
    published_at TEXT,
    web_url      TEXT
);
CREATE INDEX IF NOT EXISTS idx_episode_feed_pub ON episode(feed_id, published_at);

CREATE TABLE IF NOT EXISTS digest (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    ran_at  TEXT NOT NULL DEFAULT (datetime('now')),
    kind    TEXT NOT NULL                        -- sent | quiet
);
CREATE INDEX IF NOT EXISTS idx_digest_user ON digest(user_id, ran_at);

CREATE TABLE IF NOT EXISTS digest_item (
    id          INTEGER PRIMARY KEY,
    digest_id   INTEGER NOT NULL REFERENCES digest(id) ON DELETE CASCADE,
    episode_id  INTEGER NOT NULL REFERENCES episode(id),
    score       INTEGER NOT NULL,
    reason_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_item_digest ON digest_item(digest_id);
CREATE INDEX IF NOT EXISTS idx_digest_item_episode ON digest_item(episode_id);
"""

TABLES = ("user", "interest", "candidate_show", "episode", "digest", "digest_item")


def connect(path=None) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows."""
    conn = sqlite3.connect(path or config.DB_PATH)
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


if __name__ == "__main__":
    init_db()
    print(f"initialised {config.DB_PATH}")
    print("tables:", ", ".join(table_names()))
