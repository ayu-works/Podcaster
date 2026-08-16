# Dynamic Podcaster implementation plan

This is the executed plan. The architecture pivot from a maintained 2,500-show
universe to per-run recent category discovery was made after Step 3 and is now
the production design.

## Step 0 — external prerequisites

Local Podcast Index and Groq credentials are available and were used for live
calibration. Production remains externally blocked until:

- a Resend sending domain is verified (`onboarding@resend.dev` is owner-only),
- Turso `DATABASE_URL` and `DATABASE_TOKEN` are supplied,
- the onboarding URL is deployed and set as `PUBLIC_BASE_URL`,
- GitHub/Vercel secrets and variables are configured.

These block hosted end-to-end validation, not local implementation.

## Step 1 — config

Completed. `config.TOPICS` is the single 20-topic slug/label source. Official
Podcast Index category IDs are mapped in `TOPIC_CATEGORIES`. Dynamic discovery,
batch fetch, tagging, curation, email, database, URL, and signup limits are all
centralized.

## Step 2 — schema and clock

Completed. The nine-table schema models a shared episode pool, double opt-in,
stored tags, shared daily picks, and delivery states. Local `Path` connections
and remote libSQL URLs are distinguished safely. `last_good_cutoff()` reads
only `ok`/`partial` `fetch_cutoff_at` values.

## Step 3 — catalogue discovery

Completed, then superseded operationally by the dynamic flow. The original
global seed was implemented and saved as `data/podcast-universe.csv` for
reference. Production now queries `/recent/feeds` by official category each
run, deduplicates and balances feeds across topics, persists a diagnostic cache,
and preserves mutes.

Gate passed: all 20 topics have category mappings; dynamic merge, balance,
dedupe, cache, and mute behavior are automated tests.

## Step 4 — episode fetch and deterministic filters

Completed. Feed IDs are grouped into API requests of no more than 200, with
three request workers. The cutoff is committed before network work. GUID,
episode type, cleaned-description length, duration, and duplicate filters run
before tagging. Subscriber history is deliberately absent from this stage.

Live checks on 2026-08-16:

- 700 selected feeds: 1,717 usable rolling-24-hour episodes.
- 280 selected feeds: 872 usable rolling-24-hour episodes.
- 702 episodes in the saved Asia/Kolkata calendar-day export.

Artifacts: `data/episodes-today.csv` and `data/episodes-today-ist.csv`.

## Step 5 — one-time episode tagging

Completed. Episodes are tagged in batches of 20 with 0–3 known topic slugs, a
score, and a concrete why-line. Malformed JSON and generic reasons retry within
a per-episode cap. Completed batches commit, provider failure stops the stage,
and daily budget exhaustion leaves the remaining queue untouched.

Live quality gate passed on 20 episodes: 19 accepted, one generic response
rejected, scores 20–70, 4,689 tokens, grounded reasons on accepted rows.

## Step 6 — deterministic curation

Completed. SQL selects newly tagged, fresh episodes above the relevance bar,
with per-topic and per-show caps. It does not pad quiet topics, permits one
episode in several topic lists, and gives an episode one editorial shot across
runs. The live sample produced one qualifying business/startups pick at bar 70.

## Step 7 — per-subscriber email

Completed. Delivery merges subscribed topic lists, deduplicates episodes,
enforces whole-email caps, skips pending/sent history, retries failed history,
and isolates recipients. Pending attempt rows are committed before Resend.
HTML is escaped, grouped, mobile-width, and includes tokenized and header-based
unsubscribe.

## Step 8 — onboarding

Completed. The old free-text box, background universe build, job registry,
polling endpoints, and waiting page were removed. Signup stores exact topic
slugs and uses double opt-in. Unsubscribe GET is scanner-safe and POST is
idempotent. Honeypot and per-IP throttling are present. The repository-root
`index.py` exports the Flask app through Vercel's zero-config Flask entry point.

## Step 9 — orchestration and scheduling

Completed locally. `block-7-run/run.py` executes discover/fetch -> tag ->
curate -> send, records the required metrics, and supports `--dry-run`,
`--skip-fetch`, and `--skip-tag`. Pipeline failures are `failed`; isolated
delivery failures are `partial`.

`.github/workflows/run.yml` schedules 07:00 IST on Sun/Mon/Wed/Fri, supports a
manual trigger, requires remote database state in Actions, and uploads logs on
every outcome. There is intentionally no monthly seed workflow: dynamic
category discovery refreshes every scheduled run.

Hosted gate still pending: trigger `workflow_dispatch` with the laptop off and
confirm a real second-address delivery after Step 0's external setup.

## Step 10 — consistency and regression

Completed locally. Retired per-user rank and onboarding code was removed;
READMEs, environment example, architecture, PRD, and test cases describe the
dynamic implementation. Run the regression suite with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Current result: 52 tests passing. Remaining manual gates are a phone rendering
check, live Turso named-row behavior, Vercel signup, verified-domain delivery,
and a cloud-scheduled run with no local machine involved.
