# Podcaster

Every other day, an email with the best new podcast episodes in the topics you picked.

> **Read `doc/ARCHITECTURE.md` before changing anything.** The repo is mid-migration: the
> code on disk is **v1** (per-user), the documents describe **v3** (shared pool, hosted).
> They deliberately disagree. Do not "fix" code to match the docs unless you are executing
> `doc/IMPLEMENTATION-PLAN.md`.

## Where things are

| | |
|---|---|
| `doc/ARCHITECTURE.md` | Why the system is shaped this way. **Section numbers are cited from ~20 docstrings — never renumber them.** |
| `doc/IMPLEMENTATION-PLAN.md` | Steps 0–10 to build v3, each with a gating check. |
| `doc/podcaster-prd.md` | Product decisions, what was cut and why. |
| `doc/test-cases.md` | S0–S10, matching the plan's steps. |

## Current state (v1, on disk)

Six blocks, each a folder, each a standalone CLI. **Nothing is scheduled and nothing runs end
to end** — there is no `run.py`. The two digests in `podcaster.db` were produced by running
the scripts by hand.

```
block-1-setup/      config.py, db.py, check.py
block-2-universe/   podcastindex.py, universe.py    per-user 200-show list
block-3-onboarding/ app.py (Flask, port 5001)
block-4-fetch/      fetch.py
block-5-rank/       rank.py                          per-user LLM ranking
block-6-email/      email_out.py, templates/
```

Run a block from inside its own folder:

```bash
cd block-4-fetch && ../.venv/bin/python fetch.py --email you@example.com
```

Python is `../.venv/bin/python` (3.14.6). There is no package, no `__init__.py`, no
`pyproject.toml` — **do not add one.**

## Conventions

- **`_shared.py` import shim.** Every block folder has one. It `sys.path.insert`s the earlier
  block folders and re-exports `config` and `db`. Copy the pattern from
  `block-5-rank/_shared.py`. Import `fetch`, `podcastindex` etc. from the caller, never from
  `_shared`.
- **All tunables live in `block-1-setup/config.py`.** Nothing numeric belongs in logic.
  `RELEVANCE_BAR` is the number that decides whether this feels curated or spammy.
- **`main() -> int`**, ending `if __name__ == "__main__": raise SystemExit(main())`. A
  non-zero exit is how a block says "stop, something is wrong."
- **No migration system.** `db.py` is `CREATE TABLE IF NOT EXISTS` only. A schema change
  means deleting the database file.
- **Comments explain *why* a number or guard exists**, not what the line does. Match that
  density; the existing comments are load-bearing documentation of hard-won measurements.

## The thing to understand about this codebase

**Every failure mode here is silent.** Nothing crashes. The product just quietly gets worse
while every counter looks healthy. Most of the odd-looking code exists to make one of those
failures loud, and removing it as "defensive clutter" is the main way to break this repo.

Specific examples, all intentional:

- `looks_generic()` in `rank.py` rejects "a great listen" as a **failed** output, not a weak
  one, and exits non-zero. It is the most valuable function in the repo.
- `fetch.py` raises if *every* feed fails, because a zero-candidate run is otherwise
  indistinguishable from a quiet week.
- `universe.py` refuses to replace a show list with an empty one.
- Digests are **written before sending** and marked after, so a failed send is not lost.
- `rank.py` cuts its candidate pool structurally rather than by keyword overlap, so a string
  match cannot overrule the judgement the block exists to make.

When in doubt, make a failure louder, not quieter.

## Known-wrong things (fixed in v3, still broken on disk)

Do not be surprised by these, and do not patch them piecemeal — they are Steps 2–9 of the plan:

- `user.last_run_at` is never written, so every fetch re-pulls the same 5-day window forever.
- `rank.py` only ever sees ~25 of ~180 candidates, because Groq's 8K tokens/minute cap forces
  the pool to be truncated before the model is called.
- `candidate_show` is per user, so N users means N×200 feed polls per run.
- `FROM_EMAIL` is Resend's sandbox sender: **it can only deliver to the account owner.**
- There is no unsubscribe route.
- `logs/runs.jsonl` is referenced in `config.py` and written by nothing.
- `feedparser` is in `requirements.txt` and imported nowhere — all feeds come from the
  Podcast Index API.

## v3 target, in one paragraph

Tag each new episode **once** at ingest (topics + 0–100 score + a one-line why), store the
result, and make sending a pure SQL join. Cost then scales with *episodes*, not *subscribers*.
Pipeline runs on GitHub Actions, database is Turso (libSQL — same SQL dialect, so queries port
unchanged), signup is Vercel. All free tiers; the only cost is that Groq's 200K tokens/day caps
the show universe at ~2,500.

## Gotchas

- Port **5001**, not 5000 — macOS AirPlay owns 5000.
- Podcast Index searches **show names, not episode contents**. This one limitation shapes the
  entire design.
- `.env`, `*.db` and `logs/` are gitignored. Never commit a key.
- `doc/` filenames contain no spaces; `test case.md` was renamed to `doc/test-cases.md`.
