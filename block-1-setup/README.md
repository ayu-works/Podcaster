# Block 1: Setup

Config, schema, and the environment every later block runs on.

## Layout convention

Each block gets its own folder. Three things are shared at the project root so a
block never has to rebuild them:

```
Podcaster/
  .venv/            one virtualenv for every block
  .env              real keys, gitignored
  podcaster.db      the SQLite file
  logs/             rank.log, runs.jsonl (created by later blocks)
  block-1-setup/    config.py, db.py, check.py   <- you are here
  block-2-.../      imports config and db from block 1
```

Later block folders reach back to this one with:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "block-1-setup"))
import config, db
```

## Files

| File | What it holds |
|---|---|
| `config.py` | Every tunable from ARCHITECTURE section 9, plus paths and secrets. `RELEVANCE_BAR` is the number that matters. |
| `db.py` | The six-table schema from ARCHITECTURE section 5, `connect()`, `session()`, `init_db()`. |
| `check.py` | The Block 1 check. |

## Run it

```bash
cd "block-1-setup"
../.venv/bin/python db.py      # create podcaster.db
../.venv/bin/python check.py   # the check
```

## Check

`sqlite3 ../podcaster.db ".tables"` lists six tables:
`candidate_show  digest  digest_item  episode  interest  user`

## Before Block 2

1. Fill in `.env` at the project root (copy from `.env.example`). Use Resend's
   sandbox sender, `onboarding@resend.dev`; it needs no domain or DNS setup and
   can deliver to the email address associated with your Resend account.
2. Write 4 to 6 interest strings in your own words in `interests.txt` at the
   project root. **Specific beats broad.** "AI agents in production, not demos"
   ranks far better than "AI", because that literal text goes into both the
   search expansion and the ranking prompt.
