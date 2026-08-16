"""Block 1 check: deps import, env is loaded, and the nine tables exist."""

import importlib.util
import sys

import config
import db

DEPS = ("httpx", "feedparser", "groq", "jinja2", "resend", "flask", "dotenv")
# turso_serverless is deliberately NOT in DEPS: it is only required on the
# remote branch of db.connect(). Requiring it here would fail a local-only
# check for a package the SQLite fallback does not need; db.connect() reports
# remote-driver problems loudly when a hosted database is used.


def main() -> int:
    ok = True

    missing_deps = [name for name in DEPS if not importlib.util.find_spec(name)]
    if missing_deps:
        print(f"FAIL  deps missing: {', '.join(missing_deps)}")
        ok = False
    else:
        print(f"ok    deps ({len(DEPS)}) importable")

    db.init_db()
    found = db.table_names()
    missing_tables = [t for t in db.TABLES if t not in found]
    if missing_tables:
        print(f"FAIL  tables missing: {', '.join(missing_tables)}")
        ok = False
    else:
        # Never config.DB_PATH.name unconditionally: DATABASE_URL can point
        # init_db() at Turso or a throwaway local file, and naming the wrong
        # target is worse than naming none. Never print DATABASE_TOKEN.
        target = config.DATABASE_URL or config.DB_PATH.name
        print(f"ok    {len(db.TABLES)} tables in {target}: {', '.join(found)}")

    # Not fatal for Block 1 — nothing calls out yet — but each block is blocked
    # until its own keys are filled in.
    print("\n.env")
    for block, names in config.KEYS_BY_BLOCK.items():
        missing_env = config.missing_keys(block)
        if missing_env:
            print(f"  block {block}: needs {', '.join(missing_env)}")
        else:
            print(f"  block {block}: ok ({', '.join(names)})")

    print("\nBlock 1 " + ("passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
