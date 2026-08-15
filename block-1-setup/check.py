"""Block 1 check: deps import, env is loaded, and the six tables exist."""

import importlib.util
import sys

import config
import db

DEPS = ("httpx", "feedparser", "groq", "jinja2", "resend", "flask", "dotenv")


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
        print(f"ok    {len(db.TABLES)} tables in {config.DB_PATH.name}: {', '.join(found)}")

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
