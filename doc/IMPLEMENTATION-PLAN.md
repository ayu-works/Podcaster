# Podcaster: Step by Step Build

~4 hours. Read `ARCHITECTURE.md` first, it explains the why. This is the do.

**Every step ends with a check.** Do not skip them. The failure mode of this product is silent (mediocre picks), not loud (a crash), so the checks are the build.

This plan rebuilds v1 around a shared episode pool. Some steps are additive, but several are **deletions** — the per-user universe, the ranker's pool truncation, the onboarding background thread. Deleting them is the work, not a cleanup afterwards.

---

## Before you start

### Accounts and keys

1. **Podcast Index** at podcastindex.org/api/docs. Key and secret.
2. **Groq** at console.groq.com. API key.
3. **Resend** at resend.com. API key, plus a verified domain (see below).
4. **Turso** at turso.tech. Create a database; take the URL and auth token.
5. **GitHub** repository for this code. All six secrets go in Settings → Secrets → Actions.

Everything here is a free tier. The only thing that costs you is reach: `SHOW_TARGET` stays at ~2,500 shows because of Groq's daily token cap (see below).

### This runs in the cloud, not on your machine

The pipeline runs on **GitHub Actions** and the database is **hosted on Turso**. Nothing runs on your laptop, and nothing is stored there.

This reverses v1's launchd guidance deliberately. See `ARCHITECTURE.md` section 7 for the full reasoning; the short version is that a laptop that is shut or travelling produces no run at all, and subscribers are not told why.

The consequence that shapes the build: **an Actions runner is ephemeral.** Every run is a fresh VM, destroyed at the end. A local `podcaster.db` would not survive one run, which is why the database is remote from Step 2 onward.

### On Resend — read this before Step 0

v1 used the sandbox sender `onboarding@resend.dev`, which needs no setup at all. It has one restriction:

> **It can only deliver to your own Resend account email address.**

For a personal tool that was exactly right. **For this build it is a hard blocker.** Everything past a single subscriber is untestable until you own a verified domain — Step 7 cannot be checked, Step 9's multi-subscriber verification cannot run, and you will not discover this until the moment you try to add a second address and get a 403.

So: buy a domain, add the SPF and DKIM records Resend gives you, and set `FROM_EMAIL` accordingly. **Do it before Step 0**, because DNS propagation is dead time you want spent in parallel with the build rather than at the end of it. Nothing in the code changes; only the `from` line.

Free tier is 3,000 emails a month.

### On wall clock and the daily token cap

Tagging has **two** ceilings on Groq's free tier, and the second one is the one that bites.

`openai/gpt-oss-120b` free tier is **8K tokens/minute and 200K tokens/day** ([Groq rate limits](https://console.groq.com/docs/rate-limits)). TPM sets the pace — roughly 2 calls a minute, so a full pass is 20 to 30 minutes. **TPD sets the size of the product**, because it caps how many episodes can be tagged per day at around 770, which caps the universe at roughly 2,500 shows. `ARCHITECTURE.md` section 9 shows the arithmetic.

An earlier draft of this plan specified 5,000 shows. That run would have needed ~308K tokens and **could not have completed in a single day**, let alone four times a week. If you are on a paid tier, redo the section 9 arithmetic and raise `SHOW_TARGET`; nothing else changes.

Build the `--limit` flag first so you are never waiting on a full pass to test a change.

---

## Step 0: Prerequisites and reset (10 min)

The existing database holds one user, 190 episodes and a noisy 200-show universe, none of it worth migrating. `db.py` is `CREATE TABLE IF NOT EXISTS` only — there is no migration system, so an in-place `ALTER` is not an option.

```bash
rm -f podcaster.db podcaster.db-wal podcaster.db-shm
```

**Check:** verified sending domain is live in Resend, `.env` has all five keys, the old database file is gone.

---

## Step 1: Config (15 min)

`block-1-setup/config.py`.

**Add** `TOPICS` as `(slug, label)` pairs — the 20 labels currently hardcoded as `CHIPS` in `block-3-onboarding/app.py`, which now becomes the single source instead. Derive `TOPIC_SLUGS` and `TOPIC_LABELS` from it.

**Add:** `SHOW_TARGET = 2500`, `PICKS_PER_TOPIC = 10`, `MAX_PER_EMAIL = 10`, `MAX_PER_SHOW_PER_EMAIL = 2`, `CURATE_MAX_PER_SHOW = 2`, `CURATE_MAX_AGE_DAYS = 7`, `TAG_BATCH_SIZE = 20`, `TAG_MAX_TOPICS = 3`, `TAG_MAX_ATTEMPTS = 3`, `GROQ_TPD = 200_000`.

`SHOW_TARGET` is **derived from `GROQ_TPD`**, not chosen. See `ARCHITECTURE.md` section 9 before changing it — raising it without raising the tier silently truncates coverage, because episodes get fetched and then never tagged.

`MAX_PER_SHOW_PER_EMAIL` and `CURATE_MAX_PER_SHOW` are both needed and are not redundant. The first caps a show per email, the second caps it per topic list. Without the first, a subscriber to four topics can receive eight episodes from one show.

**Remove:** `PICKS_PER_EMAIL`, `UNIVERSE_TARGET`, `RANK_PROMPT_TOKENS`, `RANK_MAX_PER_SHOW`.

**Keep unchanged:** `RELEVANCE_BAR`, `TERMS_PER_INTEREST`, `MIN_DESC_CHARS`, `DESC_TRUNCATE`, `MAX_LOOKBACK_DAYS`, `FEED_WORKERS`, all `UNIVERSE_*` filters, `SEARCH_RESULTS_PER_TERM`, `MIN_EPISODE_SEC`, `EPISODES_PER_FEED`, `GROQ_TPM`, all paths and secrets.

`TOPICS` must not be duplicated anywhere else. It drives seeding, the tagging prompt, curation, and the onboarding checkboxes. Four copies of a topic list is four chances to drift.

**Check:** `python -c "import config; print(len(config.TOPIC_SLUGS))"` prints 20, and no slug contains a space or an uppercase letter.

---

## Step 2: Schema (20 min)

`block-1-setup/db.py`. Replace `SCHEMA` and `TABLES` with the nine tables from `ARCHITECTURE.md` section 5. Leave `connect()`, `session()`, `init_db()` and `table_names()` alone — they are fine.

**Change `connect()` to talk to Turso**, and change nothing else:

```python
def connect(url=None, token=None):
    conn = libsql.connect(url or config.DATABASE_URL,
                          auth_token=token or config.DATABASE_TOKEN)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

`session()`, `init_db()`, `table_names()` and every query in blocks 2 through 7 are untouched — libSQL is SQLite, which is the entire reason it was chosen over Postgres. Drop the WAL pragma; it is a local-file concern. Keep a `--local` flag or a `DATABASE_URL=file:...` fallback so tests can run against a throwaway file.

Add `last_good_cutoff(conn)`, returning `MAX(fetch_cutoff_at) FROM run WHERE status IN ('ok','partial')`. This is the clock.

**It must read `fetch_cutoff_at`, not `finished_at`.** `fetch_cutoff_at` is stamped at the *start* of a run, before fetch polls anything; `finished_at` is stamped 20 to 30 minutes later, after tagging. Using the later timestamp means every episode published during a run falls into a window that no run ever covers, and is lost permanently. Capturing the cutoff first costs a few duplicate rows that the `guid` upsert absorbs. **Overlap is safe, gaps are not** — this principle recurs throughout the build.

`partial` is included because a run whose fetch, tag and curate all succeeded has genuinely covered its window; only some deliveries failed, and those retry through `sent`, not by rewinding the pipeline.

This replaces `user.last_run_at`, which in v1 was never written to at all — every fetch silently re-pulled the same five-day window forever, and nothing would have told you.

Update `block-1-setup/check.py` to assert nine tables.

**Check:** `../.venv/bin/python db.py` twice in a row. Both succeed, nine tables listed. Insert the same `guid` twice and confirm one row survives. Insert a run with `status='failed'` and confirm `last_good_cutoff()` ignores it.

---

## Step 3: The global universe (30 min)

`block-2-universe/universe.py`. The machinery is good and measured — reuse `expand_interests()`, `search_all()`, `rank_feeds()` and `allocate()` **unchanged**. What changes is scope.

1. Build the interest list from `config.TOPICS` instead of one user's free text. Runs **once**, not per signup.
2. `allocate(ranked, interest_count=20, target=SHOW_TARGET)` — the round-robin draft now spreads across 20 topics. This is what stops Technology & AI eating the universe. `block-2-universe/README.md` records a three-interest profile returning 55 AI shows to 11 cooking, which is exactly the failure it prevents.
3. `save_candidate_shows(conn, user_id, feeds)` becomes `save_shows(conn, feeds)`. Drop `user_id`. Keep verbatim the upsert plus delete-not-in behaviour, the refusal to replace with an empty list, and the preservation of `status` so a muted show stays muted. Also write `show_topic` rows from `FeedHit.matched_interests`.
4. Delete `ensure_user()`, `save_interests()` and `build(conn, user_id, interests)`. Signup no longer builds anything.
5. CLI becomes `universe.py --seed-global [--target N] [--dry-run] [--use-file-terms]`.

**Check — the highest-value ten minutes in the build.** Print 50 show titles across 5 topics and **read them**. Then:

```sql
SELECT topic, COUNT(*) FROM show_topic GROUP BY topic ORDER BY 2;
```

Every topic must have shows. A topic near zero means its search terms failed, and no amount of downstream prompt tuning will rescue it — those subscribers will simply never receive anything.

v1's equivalent check earned its billing: the live universe it produced contained "Roofer Growth Hacks", "Elder Scrolls Lorecast" and "Blockchain DXB", and nothing in the product reported a problem.

---

## Step 4: Fetch (20 min)

`block-4-fetch/fetch.py`. Mostly de-scoping. Reuse `since_timestamp()`, `clean_text()`, `to_episode()`, `fetch_feeds()`, `upsert_episodes()` and the 15-worker pool as-is.

- **Stamp `run.fetch_cutoff_at = now()` before polling anything.** This is the boundary the next run reads. Writing it after the fetch, or letting `finished_at` serve as the clock, opens a 20-to-30-minute hole on every run through which published episodes vanish permanently.
- `load_shows(conn, user_id)` → `load_shows(conn)`: every `show` with `status = 'active'`.
- `fetch_for_user(conn, user_id, since)` → `fetch_all(conn, since=None)`, deriving `since` from `db.last_good_cutoff()`.
- **Remove the already-sent join.** Dedupe is now per subscriber at send time. An episode one person has seen must stay available to everyone else — this is the single most important line to delete in this step, and getting it wrong makes the pool shrink as the subscriber list grows.
- Keep the description-under-100 and duration-under-180 filters and the returned `Counter` of drop reasons.
- Keep the "every feed failed → raise `FetchError`" guard. It matters more now: a global failure would look like a quiet day for every subscriber simultaneously.
- Drop `--email` and the "STOP under 30 candidates" exit — that threshold was calibrated for one user's 200 shows.

**Check:** first run yields several hundred episodes. Run it again immediately: **~0 new.** If the second run returns the same count as the first, the clock is not advancing and every subsequent run will re-tag the same episodes.

**Check the gap explicitly**, because it is invisible otherwise: complete a run, note its `fetch_cutoff_at`, insert an episode with a `published_at` between that cutoff and the run's `finished_at`, then run again. **It must be fetched.** If it is not, the clock is reading the wrong column.

---

## Step 5: Tagging (60 min)

**The biggest step on purpose. This is the product.**

New folder `block-5-tag/` with its own `_shared.py` (needs `block-4-fetch`, `block-2-universe`, `block-1-setup` — copy the pattern from `block-5-rank/_shared.py`).

**Delete:** `select_pool()`, `estimate_tokens()`, `build_prompt()`, `rank_for_user()`. Batching removes the reason pool truncation existed. This is the fix for v1's worst property — the ranker saw ~25 of ~180 candidates, so the stage the entire product depended on was structurally blind to 85% of its input.

**Keep verbatim:** `looks_generic()`, `_GENERIC_PHRASES`, `_WORD_RE`, `format_duration()`, `log_call()`, and the retry-once-with-the-parse-error-appended pattern.

> `looks_generic()` is the most valuable function in the existing codebase. It is the automated enforcement of "generic praise is a failed output, not a weak one" — a rule that is otherwise just a hopeful sentence in a prompt. It must survive the rewrite intact.

Build `tag.py` from the prompt in `ARCHITECTURE.md` section 6, Stage 2. Batch 20. `temperature=0`, JSON mode.

**Work queue:** `WHERE tagged_at IS NULL AND tag_attempts < TAG_MAX_ATTEMPTS`. **Increment `tag_attempts` on every attempt**, success or failure, and write `tag_error` on failure.

Without the attempt cap, an episode whose description reliably produces a generic why-line is retried on every run, forever, at cost — and the backlog it sits in grows monotonically while looking like a transient. Three strikes, then abandon it and count it.

**Validation:** index in range, score 0–100, non-empty `why`, every slug in `TOPIC_SLUGS` (drop unknown slugs, do not fail the batch), at most `TAG_MAX_TOPICS`. A `looks_generic()` hit leaves `tagged_at` NULL for retry and is counted — it must not fail the run, unlike v1 where two picks made it fatal.

**Rate limiting has two ceilings.** A 20-episode batch is ~4,400 tokens all-in. `GROQ_TPM = 8000` paces you at ~2 calls a minute; honour `Retry-After` on 429, and copy the backoff pattern from `podcastindex._get()`.

`GROQ_TPD = 200_000` is the harder limit. **Track cumulative tokens across the run and stop cleanly when the daily budget is spent**, leaving the remainder untagged for the next run, rather than failing mid-batch on a 429 you cannot retry your way out of. If `untagged_left` is non-zero every single run, the universe has outgrown the tier and coverage is being silently truncated — see `ARCHITECTURE.md` section 9.

Commit per batch. This is why `tagged_at` is per episode: a killed run must resume at episode 601, not restart at 1.

CLI: `tag.py [--limit N] [--dry-run]`. Keep logging every prompt and raw response.

**Check — the real gate on the entire build.** Run `--limit 100`, then **read 20 tagged rows by hand:**

- Are the topics right? Would you have picked the same ones?
- Does each `why` name something concrete from that episode's description — a guest, a claim, a number — or does it say "a great listen for anyone interested in AI"?
- **Is the score spread real, or is everything 78 to 82?**

That last one is new, and it is the one most likely to go wrong invisibly. A flat distribution means the model is not discriminating, the bar is doing nothing, and curation has quietly degenerated into recency ordering — while every count in your logs still looks healthy.

A generic reason is a bug, not a weak output. Tighten the prompt and rerun before moving on. Everything downstream is worthless if this stage is mediocre, and it is the only part you cannot fix later by changing a number in config.

---

## Step 6: Curation (20 min)

`block-5-tag/curate.py`. **No AI.** One query per topic, `score >= RELEVANCE_BAR`, ordered by score then recency. In Python, keep at most `CURATE_MAX_PER_SHOW` per `feed_id`, then take `PICKS_PER_TOPIC`. Write to `daily_pick` with rank from 1.

**Select on `tagged_at`, not `published_at`** — this is the subtle one:

```sql
AND e.tagged_at > :previous_cutoff        -- newly tagged, whenever published
AND e.published_at > :staleness_floor     -- but not ancient
```

Tagging retries mean an episode fetched Monday may not be tagged until Wednesday. If curation filtered on publication date inside the current run's fetch window, that episode would be tagged successfully and then dropped before reaching any list — it missed its only chance, and no counter would report the loss. Selecting on when an episode *became eligible* rather than when it was *published* gives every episode exactly one shot, on the run that finished tagging it.

`:staleness_floor` is `now() - CURATE_MAX_AGE_DAYS`, and exists only so a long backlog cannot surface three-week-old episodes as today's picks.

The per-show cap and the bar live in code, not in the prompt. A prompt instruction is a request; a post-filter is a guarantee.

A topic with nothing above the bar writes **zero rows**. That is "if nothing's good, it sends you nothing" as a `WHERE` clause. Do not backfill, do not lower the bar to fill a list.

**Check:** every topic ≤ 10 rows, ≤ 2 per show, all scores ≥ 70. On a quiet run, at least one topic is legitimately empty.

**Check the retry path**, which is the failure this step exists to prevent: force one episode to fail tagging on run 1, let it succeed on run 2, and confirm it **appears in run 2's `daily_pick`** despite having been published inside run 1's window.

---

## Step 7: Email (30 min)

`block-6-email/email_out.py`. Now loops over subscribers.

Per active subscriber: join `subscription` to this run's `daily_pick`, drop anything in `sent` with status `pending` or `sent`, dedupe by episode id (one episode can appear under two of their topics), apply `MAX_PER_SHOW_PER_EMAIL`, group by topic for display, cap at `MAX_PER_EMAIL`. Empty result means **no email** — never an empty send.

**Keep the record-then-send discipline, and keep the state machine that makes it safe.** v1 had `digest.kind` with `pending` / `sent` / `failed`; `sent` must carry the same three states, or the ordering is worse than useless:

```
1. INSERT sent rows, status='pending', attempts+1   -- COMMIT here
2. call Resend
3. UPDATE to 'sent' (+sent_at) or 'failed' (+last_error)
```

Step 1 commits before step 2 because sending is irreversible. The dedupe query excludes `pending` and `sent` but **not** `failed`, so a bounced delivery becomes eligible again next run. `pending` counts as sent — deliberately conservative, inherited from v1: a crash between commit and API call is indistinguishable from one after it, and a duplicate email is worse than a missed one.

Writing bare rows with no status, as an earlier draft of this plan specified, means a Resend timeout is recorded as a successful delivery and those episodes are excluded from the retry forever. That is a regression from v1, not a simplification of it.

**Delivery is per-recipient isolated.** One subscriber failing does not abort the others; the run ends `status='partial'`. See Step 9.

Drop `digest` and `digest_item` entirely.

`templates/digest.html`: keep the 600px table layout, inline CSS, no external images (Gmail strips `<style>` blocks and clips at 102KB). Add a topic heading above each group and an **unsubscribe link** in the footer from `subscriber.unsub_token`. Update the footer copy — "Two picks at most, four mornings a week" is no longer true.

**Check:** `--dry-run --open`, then open the real thing **on your phone**. That is where you will actually read it, so that is where it has to look right. Confirm the unsubscribe link is present and correct.

---

## Step 8: Onboarding (25 min)

`block-3-onboarding/app.py`. Largely deletion.

**Delete:** `CHIPS` (use `config.TOPICS`), `parse_interests()`, `MAX_INTERESTS`, `MIN_CHIPS_WITHOUT_TEXT`, the free-text textarea, `run_build()`, `JOBS`, `JOBS_LOCK`, `_set()`, `/status/<job_id>`, `/done/<job_id>`, `done.html`, and the polling JS in `onboard.html`.

All of the job machinery existed to cover the ~30-second per-user universe build. Nothing is built at signup any more, so all of it goes. `parse_interests()` in particular deliberately *rejected* bare chip labels and demanded prose — exactly backwards now.

`POST /subscribe`: validate email, require ≥ 1 topic, insert `subscriber` with `status='pending'` and two `secrets.token_urlsafe(32)` tokens (confirm + unsubscribe), insert `subscription` rows, send the confirmation email, render "check your inbox". Synchronous. Re-subscribing an existing email updates their topic set rather than erroring.

`GET /confirm/<token>`: flip `pending` → `active`, stamp `confirmed_at`. Idempotent.

`GET /unsubscribe/<token>`: render a page with a button. **Change nothing.**
`POST /unsubscribe/<token>`: perform it. Idempotent, never a 500, never reveals whether the token was real.

### Two things a public form needs that a personal tool did not

**Double opt-in is not optional.** An unauthenticated endpoint that immediately mails whatever address it is given is a way to spam a stranger. Pending rows are inert because the send stage filters on `active`, so a hostile signup costs one confirmation email and nothing more. Skipping this also risks the sending domain: people who never signed up mark it spam, and enough of that degrades deliverability for every real subscriber.

**`GET /unsubscribe` must not mutate.** Gmail, Outlook and corporate security gateways **prefetch links in email** to scan them. A `GET` that acts on retrieval will unsubscribe people no human ever removed, and there will be no record of why they vanished. `GET` renders, `POST` acts — the ordinary HTTP contract, and ignoring it here is quietly destructive.

Also emit `List-Unsubscribe` and `List-Unsubscribe-Post` headers from `email_out.py`, so Gmail's native unsubscribe button appears. It uses one-click POST (safe from prefetch) and its presence improves inbox placement.

Light abuse control: a hidden honeypot field and a per-IP throttle on `POST /subscribe`. Nothing more until abuse actually appears.

### Deploying it

**Vercel, Python serverless, free Hobby tier.** `api/index.py` exports the Flask `app`; the existing `_shared.py` shim puts `block-1-setup/` on the path so `config` and `db` import unchanged. Same six secrets as the workflow, set in Vercel's project settings.

Reach Turso over its **HTTP API**, not a persistent connection — a serverless function is spun up per request and cannot hold a pool.

**Not Render**, the obvious "just deploy Flask" choice: free instances spin down and cold-start in roughly **50 seconds**. Irrelevant for a batch job, fatal for a signup link someone taps from a tweet. Vercel cold-starts in about a second.

Keep port 5001 for local dev (5000 is AirPlay) and the existing `style.css`.

You are a designer. This is the one step where that helps, so make it look like something you would show someone.

**Check:** submit the form on the deployed URL. It returns **immediately** — no spinner, no polling. The row is `pending`, the confirmation email arrives, clicking it flips to `active`, and only then does a digest reach that address.

**Then check the prefetch case, which is the one that bites silently:** `curl` the unsubscribe URL. The subscriber must **still be subscribed** afterwards.

---

## Step 9: Orchestrate and schedule (30 min)

New `block-7-run/` with its own `_shared.py`.

`run.py` runs fetch → tag → curate → send in order, stamping `fetch_cutoff_at` before the first stage.

**Failure semantics differ by stage, and conflating them is how half-built digests get sent:**

| Stage | On failure | Run status | Clock |
|---|---|---|---|
| 1–3 fetch, tag, curate | Stop the run immediately | `failed` | Does **not** advance |
| 4 send | Isolate to that recipient, continue with the rest | `partial` | **Does** advance |

A `partial` run advances the clock because fetch, tag and curate genuinely covered the window — rewinding would re-tag work already paid for. Failed recipients retry through their own `sent` rows with `status='failed'`, not by replaying the pipeline. This is why `last_good_cutoff()` reads `status IN ('ok','partial')`.

Append one JSON line per run to `config.RUN_LOG_PATH` (`logs/runs.jsonl`) — already defined in config and written by nothing today. Use the shape in `ARCHITECTURE.md` section 10, including the score percentiles.

CLI: `run.py [--dry-run] [--skip-fetch] [--skip-tag]`, so you can iterate on later stages without re-paying 25 minutes for earlier ones.

**GitHub Actions, in the cloud.** Two workflows:

```yaml
# .github/workflows/run.yml
name: digest
on:
  schedule:
    - cron: '30 1 * * 0,1,3,5'    # 07:00 IST = 01:30 UTC, Sun/Mon/Wed/Fri
  workflow_dispatch:               # manual trigger, for testing

jobs:
  digest:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r block-1-setup/requirements.txt
      - run: python block-7-run/run.py
        env:
          PODCASTINDEX_KEY:    ${{ secrets.PODCASTINDEX_KEY }}
          PODCASTINDEX_SECRET: ${{ secrets.PODCASTINDEX_SECRET }}
          GROQ_API_KEY:        ${{ secrets.GROQ_API_KEY }}
          RESEND_API_KEY:      ${{ secrets.RESEND_API_KEY }}
          DATABASE_URL:        ${{ secrets.DATABASE_URL }}
          DATABASE_TOKEN:      ${{ secrets.DATABASE_TOKEN }}
      - if: always()
        uses: actions/upload-artifact@v4
        with: { name: logs, path: logs/ }
```

`.github/workflows/seed.yml` is the same shape on `cron: '0 2 1 * *'`, running `universe.py --seed-global`. The monthly rebuild is a requirement (PRD R1), so it gets a workflow rather than a sentence in a document — without it the universe ages, coverage decays, and it presents as declining pick quality, so you will debug the tagger instead.

**Cron here is UTC.** 07:00 Asia/Kolkata is 01:30 UTC, and that offset crosses a date boundary, so the weekday list shifts. Get this wrong and the digest lands at the right clock time on the wrong days.

Budget: ~30 min × 4 runs ≈ **120 minutes/month** against 2,000 free on a private repo, unlimited public. The tagging stage is mostly sleeping against the Groq rate limit, but Actions bills wall clock, so that idle time is the bill.

**Two failure modes to design around, both silent:**

- **Schedule drift.** A 01:30 job can start 10–30 minutes late under load. Fine here; do not build anything that assumes precise timing.
- **Auto-disable.** GitHub disables scheduled workflows after roughly 60 days of repository inactivity. The digest then stops, no email goes out, and nothing announces it — no log will contain the failure, because no run occurs. This is the most likely way the product dies quietly. Monitor for a *gap* in `runs.jsonl`, not for an error inside it, and confirm the current policy in GitHub's docs rather than trusting this line.

Upload `logs/` as an artifact on every run, including failures. On an ephemeral runner it is otherwise destroyed with the VM, and `rank.log` is the file you will want most when tags look wrong.

**Check:** run twice back to back — the second sends nothing and writes no new `sent` rows. Then kill `tag.py` mid-run and confirm `run.status='failed'`, the clock did not move, and re-running resumes on the untagged remainder. Then force one recipient's send to fail and confirm the others still receive email, the run is `partial`, the clock **did** advance, and that recipient's episodes are re-offered next run.

**The check that matters most for this step:** trigger `run.yml` from the Actions tab with **your laptop shut**, and confirm a real email arrives. That is the entire requirement — the product runs without you — and it is the one thing no local test can demonstrate.

**Final check:** create three subscribers with overlapping topics and run clean. Read the email as a subscriber rather than as the person who built it.

**The only question that matters: would you actually play any of those episodes?**

If no, the tagger needs work and nothing else does. Go back to Step 5.

---

## Step 10: Documentation (30 min)

All planning documents live in `doc/`. Before this rewrite they disagreed three ways — the PRD described follows, a weekly budget, feedback links and 8 tables; `ARCHITECTURE.md` opened with "discovery only" and 6 tables; the code implemented neither fully. `test-cases.md` carried a "Coverage gaps" section that existed solely to enumerate the contradictions.

Keep them in sync from here. Specifically:

- **`ARCHITECTURE.md` section numbers are load-bearing.** About twenty docstrings cite them (`config.py` "section 9", `db.py` "section 5", `fetch.py` "section 6", `podcastindex.py` "section 2"). Rewrite contents, never numbering.
- Update the per-block `README.md` files, which describe the old per-user behaviour. `block-2-universe/README.md` holds the measured catalogue findings (~70% of feeds stale, near-zero term overlap, 1,641 feeds → 460 usable) — **keep those numbers**, they are hard-won, and re-measure at `SHOW_TARGET` scale.
- Fix stale docstrings: `db.py:1` says "six-table schema"; `block-1-setup/README.md:33` says the same; `block-3-onboarding/README.md:42` quotes section 8 on free text being the real signal.
- Delete `feedparser` from `requirements.txt` — listed and dependency-checked, but never imported anywhere, since all feed access goes through the Podcast Index API. Delete the unused `LLM_PROVIDER` env var.
- `interests.toml` holds three fallback interests (Cooking / AI automation / Design) that match nothing any more. Repoint it at `config.TOPICS` or delete it.
- `pitch-slides.md` slide 2 claims "chosen for you" and "in your own words". Neither is true. Slides 1 and 3 survive untouched.

**Check:** no document contradicts another on table count, scope, or cadence. Every `ARCHITECTURE.md` section number cited in a docstring still resolves to a section about the same subject.

---

## If you run long

Cut in this order:

1. **The monthly seed workflow** — re-seed by hand for the first month
2. **Topic granularity beyond 20** — ship the 20 you have
3. **`--dry-run` and `--skip-*` flags** — convenience, not correctness
4. **Concurrency tuning in fetch** — let it be slow

**Never cut:** the Step 3 seed hand-read, the Step 5 tag hand-read, the relevance bar, the record-then-send state machine in Step 7, or **the Step 9 scheduling workflow**.

That last one changed with the move to Actions. When this ran on your laptop, "run it by hand for a week" was a legitimate corner to cut. It is not any more: subscribers are waiting on it, and a pipeline nobody triggers sends nobody anything. The scheduler stopped being deployment convenience and became the delivery mechanism.

---

## First week after

Read `logs/runs.jsonl` every run.

- **`fetched` very low** → universe too narrow or the API is failing, not a tagger problem
- **`untagged_left` growing run over run** → the rate limiter is losing; the backlog compounds silently
- **`score_p90 − score_p50` under 15** → the tagger is not discriminating. Fix this before anything else; every other number will look fine while the product quietly stops working
- **Many topics at zero picks** → bar too high, or those topics are under-covered. Cross-check `show_topic` counts to tell which
- **Every topic at the cap** → bar too low, filler is leaking in. The worse direction

**Tune `RELEVANCE_BAR` before touching anything else.** It is still the single number that decides whether this feels curated or spammy — and it now decides it for every subscriber at once.

Do not add features for two weeks. Get the tags right first.
