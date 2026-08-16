# Podcaster architecture

## 1. Product and catalogue boundary

Podcaster sends up to ten genuinely useful, newly released podcast episodes in
topics a subscriber selected. It sends nothing when nothing clears the bar.

Podcast Index categorizes feeds (podcast channels), not the meaning of each
episode. The production flow therefore uses categories only for broad daily
discovery and lets the tagger judge individual episodes:

1. Query `/recent/feeds` for each of the 20 mapped product topics.
2. Merge feed IDs and round-robin across topics to a bounded daily feed target.
3. Query `/episodes/byfeedid` in batches of at most 200 feed IDs.
4. Apply deterministic usability filters.
5. Tag every new episode once with topics, score, and a grounded why-line.
6. Curate shared per-topic lists, then merge them by subscriber in SQL.

This deliberately replaces a permanently maintained “all podcasts” list. The
catalogue contains millions of feeds, but a run only needs channels that are
recently active in the requested categories. `show` is now a diagnostic cache
and mute store, not the source of truth for the next run.

## 2. Shared-pool shape

The expensive unit is an episode, not a subscriber. Tagging runs once per new
episode. A subscriber adds rows to `subscription` and a SQL join at send time;
they do not add feed polling or model calls.

```text
recent category feeds
       |
       v
batched recent episodes -> deterministic filters -> shared episode table
                                                     |
                                                     v
                                               one-time tagging
                                                     |
                                +--------------------+-------------------+
                                v                                        v
                         shared topic picks                    subscriber topics
                                +--------------------+-------------------+
                                                     v
                                                   email
```

An episode may belong to several topics. A subscriber receives it once even if
they selected several of those topics. One subscriber's history never narrows
the shared fetch or another subscriber's digest.

## 3. Correctness invariants

1. **GUID is the episode identity.** Feeds edit titles; deduping by title causes
   both misses and duplicate sends.
2. **The window starts at `fetch_cutoff_at`.** Stamp it before the first network
   request. The next run reads only cutoffs from `ok` and `partial` runs.
   Overlap is absorbed by GUID upserts; a time gap loses episodes permanently.
3. **Irreversible sends are recorded first.** Commit `sent.status='pending'`
   before calling Resend, then mark `sent` or `failed`. Failed retries; pending
   does not, because its outcome is ambiguous.
4. **Tagging is a bounded queue.** `tagged_at IS NULL` means work remains and
   `tag_attempts` prevents permanently bad descriptions from consuming every run.
5. **Curation is deterministic and one-shot.** The model supplies tags and a
   score; SQL enforces threshold, staleness, topic, and per-show caps. Quiet
   topics remain empty.
6. **Delivery history is per subscriber.** A sent row for Alice never hides an
   episode from Bob.
7. **Pipeline and recipient failures differ.** Fetch/tag/curate failure stops as
   `failed` and does not advance the clock. Recipient failures are isolated;
   the run becomes `partial` and does advance.

## 4. Components and hosting

| Component | Choice | Reason |
|---|---|---|
| Catalogue | Podcast Index API | Recent feed categories and batched feed-ID episode lookup |
| Tagger | Groq JSON completion | One bounded call per episode batch |
| Database | SQLite locally, Turso/libSQL hosted | Same SQL and named-row access |
| Digest mail | Resend | API delivery and unsubscribe headers |
| Onboarding | Flask on Vercel | Immediate serverless signup and links |
| Scheduler | GitHub Actions | Runs without the owner's computer |

Production state must be in Turso. A GitHub runner's local SQLite file is
ephemeral, so `run.py` refuses to run in Actions without `DATABASE_URL`.

## 5. Data model

Nine tables carry the system:

| Table | Purpose |
|---|---|
| `subscriber` | email, distinct confirmation/unsubscribe tokens, opt-in state |
| `subscription` | selected topic slugs per subscriber |
| `show` | recently discovered feed cache and persistent mute state |
| `show_topic` | categories that surfaced each cached feed; coverage diagnostics |
| `episode` | global episode data plus score, why, tagging queue state |
| `episode_topic` | stored many-to-many episode tags |
| `run` | cutoff, counters, completion and failure taxonomy |
| `daily_pick` | shared ranked topic lists for one run |
| `sent` | per-subscriber delivery attempt state |

There is no `user_id` on `show` or `episode`, and no free-text interest table.
Topic slugs in `config.TOPICS` are the matching contract across onboarding,
tagging, curation, and delivery.

## 6. Pipeline stages

### Stage 1 — discover, fetch, filter

Create a run and commit its cutoff before outbound work. Query all official
category mappings concurrently, dedupe feed IDs, and round-robin to
`DISCOVERY_FEED_TARGET`. Preserve muted cached shows. Fetch episodes with no
more than 200 feed IDs per request.

Drop missing GUIDs, trailers/bonuses, duplicate GUIDs, descriptions shorter
than `MIN_DESC_CHARS` after HTML cleaning, and episodes shorter than
`MIN_EPISODE_SEC`. Unknown duration is allowed. Every request batch failing is
an error; partial failures are counted.

### Stage 2 — tag once

Load `tagged_at IS NULL AND tag_attempts < TAG_MAX_ATTEMPTS` in batches. Each
response contains 0–3 known topic slugs, a 0–100 score, and one reason grounded
in the supplied description. Unknown slugs are discarded. Generic reasons are
failed outputs and retry within the attempt limit.

Pace against `GROQ_TPM` and stop cleanly before `GROQ_TPD`. An unfinished
queue is visible in run metrics and resumes later.

### Stage 3 — curate

For each topic, select newly tagged, non-stale episodes at or above
`RELEVANCE_BAR`, ordered by score and publication time. Apply
`CURATE_MAX_PER_SHOW` and `PICKS_PER_TOPIC`. Replace the current run's picks on
rerun, but do not give an episode a second editorial shot in a later run.

### Stage 4 — deliver

For each active subscriber, join subscriptions to the current run's picks.
Exclude their pending/sent episodes, retain failed ones for retry, dedupe
cross-topic matches, apply `MAX_PER_SHOW_PER_EMAIL`, and cap at
`MAX_PER_EMAIL`. Empty selection means no message.

The digest is grouped by topic and uses a 600px table, inline CSS, escaped
catalogue text, no external images, a tokenized unsubscribe link, and
`List-Unsubscribe`/`List-Unsubscribe-Post` headers.

## 7. Hosted security and failure behavior

New subscribers are `pending`. Only a confirmation link makes them `active`,
so an attacker cannot enroll a stranger into recurring mail. Confirmation and
unsubscribe tokens are separate `secrets.token_urlsafe(32)` values.

`GET /unsubscribe/<token>` is read-only because mail scanners prefetch links.
Only POST mutates, and repeated or unknown POSTs return the same success page.
The public form also has a hidden honeypot and a small in-memory per-IP throttle.

The Resend sandbox sender reaches only the account owner. Multiple real users
require a verified domain, SPF/DKIM, and a production `FROM_EMAIL`.

## 8. Onboarding

`GET /` renders the 20 topics from `config.TOPICS` and an email field. There is
no free-text box: delivery matches stored slugs, so arbitrary prose has no
architectural consumer without reintroducing per-user model work.

`POST /subscribe` validates at least one known topic, stores the exact topic
set, sends confirmation synchronously, and renders immediately. Re-subscribing
an active address updates its topics. Re-subscribing a paused/unsubscribed
address rotates both tokens and requires confirmation again.

`GET /confirm/<token>` is idempotent. GET unsubscribe only renders; POST
unsubscribe mutates idempotently without revealing token validity.

## 9. Configuration and capacity

The production sizing constants are:

```text
DISCOVERY_RESULTS_PER_TOPIC = 1000
DISCOVERY_FEED_TARGET       = 240
EPISODE_FEED_BATCH_SIZE     = 200
FETCH_BATCH_WORKERS         = 3
TAG_BATCH_SIZE              = 20
GROQ_TPD                    = 200000
RELEVANCE_BAR               = 70
PICKS_PER_TOPIC             = 10
MAX_PER_EMAIL               = 10
MAX_PER_SHOW_PER_EMAIL      = 2
```

The feed target was measured rather than guessed. On 2026-08-16, 280 balanced
feeds produced 872 usable rolling-24-hour episodes, or 3.11 per feed. A target
of 240 projects roughly 747 episodes, leaving retry headroom around the free
tier's practical daily tagging ceiling. Recalibrate this ratio after material
category, cadence, or provider changes.

`SHOW_TARGET=2500` and the static seed tools remain only for reference/fallback;
they do not size or feed the scheduled pipeline.

## 10. Observability and operation

Append exactly one JSON line per attempted run to `logs/runs.jsonl` with:

```json
{"run_id":14,"fetch_cutoff_at":"...","shows":240,"fetched":747,
 "tagged":730,"untagged_left":17,"tag_abandoned":3,"tokens_used":168400,
 "score_p50":61,"score_p90":79,"picks_by_topic":{"technology-ai":10,"travel":0},
 "subscribers":340,"emails_sent":318,"emails_failed":2,"status":"partial"}
```

Watch for low fetched counts, a growing untagged queue, score-distribution
drift, repeated zero-pick topics, delivery-failure trends, and gaps where no run
line exists at all. Upload logs on both successful and failed Action jobs.

The workflow runs at `30 1 * * 0,1,3,5` UTC: 07:00 Asia/Kolkata on
Sun/Mon/Wed/Fri. There is no monthly seed workflow because discovery refreshes
from recent category feeds every run. GitHub may delay cron jobs; the cutoff
window makes that safe. A completely absent job remains the most silent failure
and must be detected by a run-log gap.
