# Podcaster

Four mornings a week, discover recently active podcast channels by category,
fetch their new episodes, tag each episode once, and email only strong matches.

Read `doc/ARCHITECTURE.md` before changing failure or delivery semantics.

## Current flow

```text
Podcast Index /recent/feeds (20 mapped topics)
  -> balance to DISCOVERY_FEED_TARGET
  -> /episodes/byfeedid (<=200 feed IDs/request)
  -> deterministic filters
  -> Groq topic/score/why tagging once per episode
  -> SQL curation per topic
  -> SQL merge per active subscriber
  -> Resend
```

There is no per-user show universe and no model call at send time. The old
2,500-show CSV in `data/podcast-universe.csv` is a reference artifact, not the
production input. Dynamic discovery refreshes on every run.

## Blocks

| Folder | Responsibility |
|---|---|
| `block-1-setup/` | config, nine-table schema, SQLite/Turso connection |
| `block-2-universe/` | Podcast Index client, dynamic category discovery, reference seed tooling |
| `block-3-onboarding/` | Flask topics form, double opt-in, unsubscribe |
| `block-4-fetch/` | batched episode retrieval and deterministic filtering |
| `block-5-tag/` | shared episode tagging and deterministic curation |
| `block-6-email/` | per-subscriber selection, rendering, safe delivery |
| `block-7-run/` | orchestration, status, metrics |

Run tests from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Run the full pipeline from `block-7-run/` with `../.venv/bin/python run.py`.
Use `--dry-run`, `--skip-fetch`, and `--skip-tag` for controlled iteration.

## Conventions and invariants

- Each block uses `_shared.py` to expose sibling folders. There is no package
  or `pyproject.toml`.
- Every tunable belongs in `block-1-setup/config.py`.
- `episode.guid` is the global dedupe key; never dedupe by title.
- Stamp `run.fetch_cutoff_at` before the first network request. Only `ok` and
  `partial` runs advance the next window.
- `tagged_at IS NULL` is the tagging queue and attempts are bounded.
- Commit `sent.status='pending'` before calling Resend. Failed is retryable;
  pending and sent are not.
- Fetch/tag/curate failures stop the pipeline as `failed`. Recipient failures
  are isolated and make the run `partial`.
- Pending subscribers receive nothing. A GET unsubscribe request never mutates.
- Make silent degradation visible: all-feed failure raises, generic why-lines
  fail validation, quiet topics are never padded, and every run logs metrics.

## Deployment blockers

- GitHub Actions needs Podcast Index, Groq, Resend, and Turso credentials.
- Vercel onboarding needs the same Turso database and Resend key.
- `onboarding@resend.dev` only reaches the Resend account owner. Multiple real
  subscribers require a verified sending domain and `FROM_EMAIL`.
- Set `PUBLIC_BASE_URL` to the deployed onboarding URL so confirmation and
  unsubscribe links do not point at localhost.

Port 5001 is used locally because macOS AirPlay occupies 5000.
