# Podcaster — Test Cases

## Scope and assumptions

These tests follow the eleven steps in `IMPLEMENTATION-PLAN.md`, using `ARCHITECTURE.md` for expected technical behavior and `podcaster-prd.md` for product-quality expectations.

**All three documents now describe the same system.** The previous version of this file carried a "Coverage gaps between the PRD and implementation plan" section, because the PRD specified followed shows, a weekly budget, feedback links and eight tables while the architecture specified discovery-only and six. That section is deleted. If it needs to come back, the documents have drifted again and that is the bug to fix first.

Use a dedicated test email address and test database. Mock Podcast Index, Groq and Resend for repeatable failure-path tests; use the real services for the manual quality gates.

**Three classes of test matter more than the rest,** because all three fail silently and none produces an error:

1. **Tag quality** (S5-11, S5-12). Every subscriber's email derives from `episode.score` and `episode_topic`. Degraded tagging makes every email worse simultaneously while every count in the logs stays healthy.
2. **Cross-subscriber isolation** (S4-06, S7-*). The pool is shared now. A bug that lets one subscriber's history narrow a shared fetch shrinks the pool for everyone as the list grows.
3. **State transitions** (S2-10, S4-14, S6-11, S7-17). Three boundaries in this system can lose data without raising anything: the run clock, the tagging retry path, and the delivery record. Each has a dedicated regression test, and each of those tests exists because the design got it wrong once already.

The rule the third class encodes: **overlap is safe, gaps are not.** Every window in this system errs toward doing work twice, because upserts are idempotent and lost episodes are not recoverable.

---

## Step 0: Prerequisites and reset

| ID | Test | Action | Expected result |
|---|---|---|---|
| S0-01 | Verified sender | Send to an address that is **not** the Resend account owner. | Delivery succeeds. A 403 means the sandbox sender is still configured and every multi-subscriber test below is invalid. |
| S0-02 | Keys present | Load `config.py` with all five variables set. | Loads cleanly; no secret is printed or logged. |
| S0-03 | Missing secret | Unset one variable and run the Step 1 check. | The missing variable and affected block are named clearly; the affected block fails fast when it starts. |
| S0-04 | Clean slate | Confirm the old database file is gone. | No `podcaster.db`. An in-place upgrade is not supported — there is no migration system. |

**Gate:** a real email reaches a second, non-owner address.

---

## Step 1: Config

| ID | Test | Action | Expected result |
|---|---|---|---|
| S1-01 | Topic list shape | Inspect `TOPICS`, `TOPIC_SLUGS`, `TOPIC_LABELS`. | 20 entries; every slug lowercase, hyphenated, no spaces; slugs unique; labels unique. |
| S1-02 | Single source | Grep the codebase for topic label strings. | They appear only in `config.py`. No copy in `app.py`, the tagging prompt, or a template. |
| S1-03 | New constants | Inspect exports. | `SHOW_TARGET` 2500, `PICKS_PER_TOPIC` 10, `MAX_PER_EMAIL` 10, `MAX_PER_SHOW_PER_EMAIL` 2, `CURATE_MAX_PER_SHOW` 2, `CURATE_MAX_AGE_DAYS` 7, `TAG_BATCH_SIZE` 20, `TAG_MAX_TOPICS` 3, `TAG_MAX_ATTEMPTS` 3, `GROQ_TPD` 200000. |
| S1-04 | Retired constants | `grep -rn 'PICKS_PER_EMAIL\|UNIVERSE_TARGET\|RANK_PROMPT_TOKENS\|RANK_MAX_PER_SHOW' --include='*.py' .` | No hits. **Scoped to executable code**: `doc/` legitimately names these constants when explaining what was removed, so an unscoped grep can never pass. |
| S1-05 | Preserved constants | Inspect exports. | Bar `70`, terms `18`, min description `100`, truncation `400`, max lookback `5`, workers `15`, `GROQ_TPM` `8000`. |
| S1-06 | Derived universe size | Inspect `SHOW_TARGET` against `GROQ_TPD`. | `SHOW_TARGET × (1/3) × ~220 tokens` fits inside `GROQ_TPD` with headroom. At the free tier's 200K/day this means ~2,500, not 5,000. |

**Gate:** 20 valid slugs, defined in exactly one place.

---

## Step 2: Schema

| ID | Test | Action | Expected result |
|---|---|---|---|
| S2-00 | Remote connection | Point `DATABASE_URL`/`DATABASE_TOKEN` at Turso and call `connect()`. | Connects; foreign keys ON. A bad token fails loudly at connect time, not silently at first query. |
| S2-01 | Initialization | Run `init_db()` on a new database twice. | Both succeed; exactly nine tables: `subscriber`, `subscription`, `show`, `show_topic`, `episode`, `episode_topic`, `run`, `daily_pick`, `sent`. |
| S2-01b | Dialect unchanged | Run the full schema and a representative query set against Turso. | `datetime('now')`, `INSERT … ON CONFLICT`, and the partial index on `episode(tagged_at)` all work unmodified. If any needs rewriting, the reason for choosing libSQL over Postgres has evaporated. |
| S2-01c | Local fallback | Run the suite with `DATABASE_URL=file:test.db`. | Works against a throwaway local file, so tests need no network and no shared remote state. |
| S2-02 | GUID uniqueness | Insert the same episode GUID twice. | The duplicate is rejected or ignored; one row remains. |
| S2-03 | Email uniqueness | Insert the same subscriber email twice. | Rejected. |
| S2-04 | Token uniqueness | Insert two subscribers with the same `unsub_token`. | Rejected. Tokens must be unguessable and unique. |
| S2-05 | Cascade | Delete a subscriber holding `subscription` and `sent` rows. | Dependent rows are removed; no orphans. Foreign keys are ON. |
| S2-06 | Composite keys | Insert the same `(episode_id, topic)` twice, and the same `(subscriber_id, episode_id)` into `sent` twice. | Both rejected. These are the dedupe guarantees the send stage relies on. |
| S2-07 | Clock, empty | Call `last_good_cutoff()` on a virgin database. | Returns NULL; the caller floors to `MAX_LOOKBACK_DAYS`. |
| S2-08 | Clock, failed runs only | Insert runs with status `running` and `failed` only. | Returns NULL. A failed pipeline run must never advance the clock. |
| S2-09 | Clock, mixed | Insert an `ok` run, then a later `failed` one. | Returns the **`ok`** timestamp, not the later failed one. |
| S2-10 | **Clock reads the right column** | Insert an `ok` run where `fetch_cutoff_at` is 30 minutes earlier than `finished_at`. | Returns **`fetch_cutoff_at`**. Returning `finished_at` opens a 30-minute window that no run ever covers — a permanent, silent data-loss bug. |
| S2-11 | Clock accepts `partial` | Insert a `partial` run. | Included. Its pipeline stages succeeded; only some deliveries failed, and those retry through `sent`. |
| S2-12 | Delivery states | Inspect the `sent` schema. | Carries `status` (`pending`/`sent`/`failed`), `attempts`, `last_error`. A bare existence row cannot distinguish a delivered email from a timed-out one. |
| S2-13 | Tag retry columns | Inspect the `episode` schema. | Carries `tag_attempts` and `tag_error`. Without the counter, a deterministically-bad description retries forever. |

**Gate:** nine tables; the clock reads `fetch_cutoff_at` and accepts `ok` and `partial` only.

---

## Step 3: The global universe

| ID | Test | Action | Expected result |
|---|---|---|---|
| S3-01 | API authentication | Freeze the timestamp; call through a mock server. | Headers carry key, timestamp and SHA1 of `key + secret + timestamp`; secrets never appear in the URL. |
| S3-02 | Show search mapping | Mock `/search/byterm` with valid results. | Parsed into feed IDs, URLs, names and recency fields. |
| S3-03 | API failure | Return a timeout, a `429` and a `500`. | Retries per policy, then fails visibly. **Never reports an empty successful universe.** |
| S3-04 | Topic expansion | Expand all 20 topics with a mocked Groq response. | Each yields 18 non-empty unique terms; terms are catalogue-style phrases, not copies of the label. |
| S3-05 | Expansion contract | Return 17 terms for one topic. | Raises rather than silently under-seeding that topic. |
| S3-06 | Feed deduplication | Return the same feed from several terms. | One `show` row for that `feed_id`; `show_topic` records every matching topic. |
| S3-07 | Recency filter | Feeds last published 59, 60 and 61 days ago. | The 60-day boundary is applied consistently; older feeds dropped. |
| S3-08 | Free filters | Include dead feeds, non-English feeds, feeds under 5 episodes, blocklisted titles, normalized-title duplicates. | All dropped before allocation. |
| S3-09 | Round-robin allocation | Supply 3,000 matches for one topic and 60 for another. | The loud topic does not monopolise. Both are represented; the draft spreads across topics. |
| S3-10 | No user scoping | Inspect the `show` table. | **No `user_id` column exists.** Two signups produce one shared universe, not two. |
| S3-11 | Empty-result guard | Make search return nothing, then re-run the seed against a populated table. | The existing universe is **not** replaced with an empty list. |
| S3-12 | Mute preserved | Mute a show, then re-seed. | It stays muted. |
| S3-13 | Topic coverage | `SELECT topic, COUNT(*) FROM show_topic GROUP BY topic`. | **Every topic has shows.** A topic near zero means its terms failed and its subscribers will never receive anything. |
| S3-14 | Manual relevance gate | Run against the real API; read 50 titles across 5 topics. | Predominantly relevant and specific. Generic or off-topic results trigger a term rewrite before proceeding. |

**Gate:** a shared universe near `SHOW_TARGET`, every topic covered, list passes a manual read.

---

## Step 4: Fetch

| ID | Test | Action | Expected result |
|---|---|---|---|
| S4-01 | Incremental window | Set the last good run to two days ago; include episodes either side. | Only episodes newer than the boundary enter the pool. |
| S4-02 | Lookback cap | Last good run ten days ago, five-day maximum. | Fetch begins no earlier than five days ago. |
| S4-03 | Missed-run recovery | Last good run three days ago. | The whole three-day gap is eligible; nothing lost to a hardcoded window. |
| S4-04 | Virgin database | No `ok` runs at all. | Falls back to `MAX_LOOKBACK_DAYS` rather than fetching all history or nothing. |
| S4-05 | GUID upsert | Same GUID returned again with an edited title. | One row remains, updated per policy. |
| S4-06 | **No already-sent filter** | Mark an episode `sent` for subscriber A, then re-fetch. | **It is still fetched and still available to subscriber B.** One subscriber's history must never narrow a shared fetch. |
| S4-07 | Short description | Descriptions of 99 and 100 characters. | 99 dropped, 100 retained. Length is measured after HTML stripping. |
| S4-08 | Trailer and duration | A trailer, a 179-second episode, a 180-second episode. | Trailer and 179s dropped; the 180s non-trailer retained. |
| S4-09 | Partial feed failure | Some feed requests time out, others succeed. | Successful feeds still produce candidates; failures logged; the run does not appear fully successful. |
| S4-10 | Total feed failure | **Every** feed request fails. | Raises `FetchError`. A zero-candidate run must not be indistinguishable from a quiet week — now it would look quiet for every subscriber at once. |
| S4-11 | Concurrency bound | Instrument simultaneous requests. | Peak concurrency never exceeds `FEED_WORKERS`. |
| S4-12 | Idempotent re-fetch | Run fetch twice back to back. | The second yields ~0 new episodes. A second run matching the first means the clock is not advancing. |
| S4-13 | **Cutoff stamped before fetch** | Instrument a run; compare `fetch_cutoff_at` against the first outbound API call. | The cutoff is written **first**. Stamping it after the poll re-opens the gap S4-14 tests for. |
| S4-14 | **Mid-run publication gap** | Complete a run. Insert an episode whose `published_at` falls between that run's `fetch_cutoff_at` and its `finished_at`. Run again. | **It is fetched.** This is the single highest-value regression test in the suite: with a 20–30 minute tagging stage, getting this wrong silently drops every episode published during every run, forever, and no counter reports it. |
| S4-15 | Overlap is harmless | Set the cutoff 10 minutes earlier than strictly necessary; re-run. | Already-seen episodes are re-fetched and absorbed by the `guid` upsert. No duplicates, no errors. Overlap is safe; gaps are not. |

**Gate:** several hundred episodes on a live run; an immediate re-run yields ~0; an episode published mid-run is still fetched; an episode sent to one subscriber remains available to others.

---

## Step 5: Tagging

| ID | Test | Action | Expected result |
|---|---|---|---|
| S5-01 | Work queue | Mix tagged and untagged episodes. | Only `tagged_at IS NULL` rows are selected. |
| S5-02 | Prompt contents | Build a prompt from known episodes. | Contains the allowed slug list, stable 1-based indexes, show/title/duration, descriptions truncated to 400 characters. |
| S5-03 | Valid response | Return valid JSON for a full batch. | Topics, scores and why-lines parsed; `episode.score`, `why`, `tagged_at` and `episode_topic` all written. |
| S5-04 | Unknown slug | Return `"quantum-basketry"` alongside two valid slugs. | The unknown slug is dropped; the valid ones are kept; **the batch is not failed.** |
| S5-05 | Topic cap | Return five topics for one episode. | At most `TAG_MAX_TOPICS` (3) are stored. |
| S5-06 | Empty topics | Return `"topics": []`. | Accepted and tagged. The episode reaches no list. This is expected and common, not an error. |
| S5-07 | Score bounds | Return scores of −5, 0, 100 and 140. | Out-of-range entries rejected; 0 and 100 accepted. |
| S5-08 | Malformed JSON recovery | First response malformed, retry valid. | Exactly one retry, with the parse error appended; then valid results. |
| S5-09 | Repeated parse failure | Both responses malformed. | The batch is left untagged and counted. **The run does not crash**, and those episodes are retried next run. |
| S5-10 | Generic reason diverted | Return a why-line of "a great listen for anyone interested in AI". | `looks_generic()` catches it, `tagged_at` stays NULL for retry, the count is recorded. **The run does not fail** — unlike v1, where this was fatal. |
| S5-11 | **Reason grounding (manual)** | Read 20 tagged rows against their descriptions. | Every `why` names something real: a guest, a claim, a case study, a number. Generic reasons block progression. |
| S5-12 | **Score distribution (manual)** | Compute p50 and p90 over ≥ 200 tagged episodes. | **p90 − p50 ≥ 15.** A flat spread means the model is not discriminating, the bar is inert, and curation has degenerated into recency ordering — while every log count still looks healthy. |
| S5-13 | Topic sanity (manual) | Read 20 rows' topics. | Assignments are defensible. Watch for collapse onto two popular slugs, which starves every other topic. |
| S5-14 | Resumability | Kill the process mid-run, then restart. | Already-tagged episodes are untouched; work resumes on the remainder. At 20–30 minutes a run, a restart-from-zero is a real failure. |
| S5-15 | Per-batch commit | Kill the process after batch 3 of 10. | Batches 1–3 are durably tagged. |
| S5-16 | Rate limiting | Instrument call timing; return a `429` with `Retry-After`. | Calls are paced against `GROQ_TPM`; `Retry-After` is honoured; backoff is exponential. |
| S5-17 | Logging | Run one successful and one malformed request. | Full prompts and raw responses are appended with enough context to diagnose; no API secrets present. |
| S5-18 | Cost independence | Tag a fixed batch with 1 subscriber, then with 500. | **Identical call count.** Tagging cost is a function of episodes, never of subscribers. This is the property the whole redesign exists to obtain. |
| S5-19 | **Attempt counter increments** | Fail one episode's tagging three times. | `tag_attempts` reaches 3, `tag_error` is populated, and the episode leaves the work queue. Without the cap it is retried on every run forever, at cost, while looking like a transient backlog. |
| S5-20 | Abandonment is counted | Let an episode exhaust `TAG_MAX_ATTEMPTS`. | Reported as `tag_abandoned` in `runs.jsonl`, not silently dropped. |
| S5-21 | Successful attempt still counts | Tag an episode successfully on the first try. | `tag_attempts` is 1, not 0. The counter measures attempts, not failures. |
| S5-22 | **Daily budget stops cleanly** | Set `GROQ_TPD` low enough to be exhausted mid-run. | Tagging **stops cleanly**, leaving the remainder untagged with `tag_attempts` unincremented for unattempted episodes. It does not fail mid-batch on an unrecoverable 429, and the run continues to curate and send what it has. |
| S5-23 | Budget exhaustion is visible | Same as S5-22. | `tokens_used` approaches `GROQ_TPD` and `untagged_left` is non-zero in `runs.jsonl`. A non-zero `untagged_left` on **every** run means the universe has outgrown the tier and coverage is being silently truncated. |

**Gate:** 20 hand-read rows are grounded and correctly tagged, the score spread is real, a killed run resumes cleanly, and futile retries are bounded.

---

## Step 6: Curation

| ID | Test | Action | Expected result |
|---|---|---|---|
| S6-01 | Score gate | Episodes scoring 69, 70 and 90 in one topic. | 69 excluded; 70 and 90 eligible. |
| S6-02 | Ordering | Mixed scores and publish dates. | Ordered by score descending, recency as tiebreak. `rank` starts at 1 and is contiguous. |
| S6-03 | Per-show cap | Five qualifying episodes from one show in one topic. | At most `CURATE_MAX_PER_SHOW` (2) survive **in that topic**. |
| S6-04 | Topic cap | Forty qualifying episodes in one topic. | Exactly `PICKS_PER_TOPIC` (10) rows written. |
| S6-05 | Empty topic | No episode in a topic clears the bar. | **Zero rows. No backfill, no bar relaxation.** This is the "sends you nothing" guarantee. |
| S6-06 | Untagged excluded | Include episodes with `tagged_at IS NULL`. | Never selected, regardless of any stale score. |
| S6-07 | Staleness floor | Include a tagged, qualifying episode published `CURATE_MAX_AGE_DAYS + 1` ago. | Excluded. A long tagging backlog must not surface three-week-old episodes as today's picks. |
| S6-11 | **Late-tagged episode is still curated** | Fail one episode's tagging on run 1; let it succeed on run 2. | **It appears in run 2's `daily_pick`**, despite having been published inside run 1's fetch window. Selecting on `published_at` instead of `tagged_at` would tag it successfully and then drop it — a silent deletion no counter reports. |
| S6-12 | Selection column | Inspect the curate query. | Filters on `tagged_at > :previous_cutoff`, with `published_at` used only as a staleness floor. |
| S6-13 | One shot per episode | Run curate twice across consecutive runs with no new tagging. | An episode curated in run N is not re-curated in run N+1. Eligibility is "newly tagged", not "recently published". |
| S6-08 | Cross-topic presence | One episode tagged with two topics, qualifying in both. | Appears in both topic lists. Per-subscriber dedupe happens at send, not here. |
| S6-09 | No AI | Instrument the Groq client during curation. | **Zero model calls.** |
| S6-10 | Run scoping | Curate twice under different `run_id`s. | Rows are scoped per run; an earlier run's picks are untouched. |

**Gate:** every topic ≤ 10 rows, ≤ 2 per show, all ≥ 70, at least one topic legitimately empty on a quiet run.

---

## Step 7: Email and multi-subscriber

| ID | Test | Action | Expected result |
|---|---|---|---|
| S7-01 | Topic match | Subscriber ticked `design` only. | Receives only `design` picks. |
| S7-02 | Multi-topic merge | Subscriber ticked three topics with picks in each. | All three appear, grouped under topic headings, capped at `MAX_PER_EMAIL`. |
| S7-03 | **Cross-topic dedupe** | One episode qualifies in two of a subscriber's topics. | It appears **once**. |
| S7-04 | **Already-sent dedupe** | An episode is in `sent` for this subscriber. | Excluded, even if it ranks in the current run. |
| S7-05 | **Shared picks** | Two subscribers, identical topics, both new. | Both receive the same episodes. This is the accepted design, not a bug. |
| S7-06 | **Divergence** | Subscriber A has been sent episode X; B has not. Both subscribe to its topic. | A does not receive X; B does. |
| S7-07 | **Mid-window signup** | Create a subscriber after the run's cutoff but before send. | They still receive this run's picks. A new subscriber inherits the pool from day one. |
| S7-08 | Empty result | A subscriber's only topic curated to zero. | **No email sent** — not an empty one, and no "nothing today" note. |
| S7-09 | Paused and unsubscribed | Set status to `paused`, then `unsubscribed`. | Skipped in both cases; no `sent` rows written. |
| S7-10 | Cap | A subscriber matches 40 picks across topics. | Exactly `MAX_PER_EMAIL` (10) sent, highest ranked first. |
| S7-11 | Rendering | Render a known set. | Each item shows title, show, human-readable duration, grounded why-line and listen link, under its topic heading. |
| S7-12 | Email constraints | Inspect the HTML. | Single column, 600px max, inline CSS, no external images, under Gmail's 102KB clip. |
| S7-13 | Escaping | Titles containing `<`, `&` and quotes. | Escaped; no markup injection. |
| S7-14 | Unsubscribe link | Inspect a rendered email. | Present in the footer, carries **that subscriber's** token, and resolves. |
| S7-15 | Record-then-send | Pause immediately before the Resend call. | `sent` rows exist, are committed, and carry `status='pending'`. A bare row with no status cannot distinguish a delivered email from one that never left. |
| S7-16 | **Send failure is isolated** | Resend times out for subscriber 2 of 3. | Subscribers 1 and 3 **still receive email** — delivery is per-recipient isolated, unlike stages 1–3 which abort the run. Subscriber 2's rows become `status='failed'` with `last_error` set, and the run ends `partial`. |
| S7-17 | **Failed send is retried** | After S7-16, run again. | Subscriber 2 is re-offered the same episodes. `failed` rows are re-eligible; this is the whole reason `sent` carries a status. |
| S7-18 | **Pending is not retried** | Leave a row at `status='pending'` (simulating a crash between commit and API call). | Those episodes are **excluded** from the next run. Deliberately conservative: a crash after the API call is indistinguishable from one before it, and a duplicate email is worse than a missed one. |
| S7-19 | Attempts counted | Fail a subscriber's send twice. | `attempts` reaches 2; `last_error` reflects the most recent failure. |
| S7-20 | **Per-show cap across the whole email** | A subscriber ticks 4 topics; one show has 2 qualifying episodes in each. | At most `MAX_PER_SHOW_PER_EMAIL` (2) appear **in the email**, not 8. The PRD promise is per email; `CURATE_MAX_PER_SHOW` alone only bounds it per topic list. |
| S7-21 | Cost per subscriber | Instrument the Groq client during send. | **Zero model calls, regardless of subscriber count.** |
| S7-22 | Mobile check (manual) | Open a real email on a phone. | Readable without horizontal scrolling, links tappable, scannable in under 45 seconds. |

**Gate:** three subscribers with overlapping topics each receive correct, deduped, non-empty email; one with a quiet topic receives nothing; a failed recipient does not block the others and is retried next run.

---

## Step 8: Onboarding

| ID | Test | Action | Expected result |
|---|---|---|---|
| S8-01 | Render | `GET /`. | `200`, 20 topic checkboxes from `config.TOPICS`, an email field, **and no free-text box.** |
| S8-02 | Valid signup | Submit a valid email and three topics. | One `subscriber` and three `subscription` rows; confirmation rendered. |
| S8-03 | **Synchronous** | Time the request. | Returns immediately. **No background thread, no job id, no status polling.** Nothing is built at signup. |
| S8-04 | Minimum topics | Submit zero topics. | Validation error; no partial records. |
| S8-05 | Invalid email | Submit a blank or malformed address. | Rejected safely; no partial records. |
| S8-06 | Unknown slug | POST a slug not in `TOPIC_SLUGS`. | Rejected or ignored; no junk row in `subscription`. |
| S8-07 | Duplicate email | Submit the same address twice with different topics. | Deterministic: the topic set is updated; no second active subscriber. |
| S8-08 | Token generation | Inspect two new subscribers. | Tokens are unique, unguessable, and long enough not to be enumerable. |
| S8-09 | Unsubscribe, two-step | `GET /unsubscribe/<token>`, then `POST` the button. | `GET` renders a confirm page and **changes nothing**. `POST` flips to `unsubscribed`; the next run skips them. |
| S8-10 | **Link prefetch is safe** | `curl` the unsubscribe URL, as Gmail/Outlook scanners do. | **The subscriber is still subscribed.** A mutating `GET` would silently remove people no human unsubscribed, with no record of why they vanished. |
| S8-11 | Unsubscribe idempotent | `POST` the same token twice, then an unknown token. | Success rendered every time. **Never a 500**, and never a hint about whether the token was real. |
| S8-12 | **Pending until confirmed** | Sign up, then run the pipeline before clicking the confirmation link. | The subscriber is `pending` and receives **no digest**. The send stage filters on `active`. |
| S8-13 | Confirmation flow | Click the confirm link. | Status flips to `active`, `confirmed_at` stamped. The next run delivers. Idempotent on a second click. |
| S8-14 | Hostile signup is inert | POST a stranger's address. | They receive **one** confirmation email and nothing further unless they click. This is the whole reason double opt-in exists: an endpoint that mails anyone on request is an abuse vector and a deliverability risk. |
| S8-15 | Tokens are distinct | Inspect a new subscriber. | `confirm_token` and `unsub_token` differ. Reusing one for both means a confirmation link can unsubscribe. |
| S8-16 | List-Unsubscribe headers | Inspect a sent email's headers. | `List-Unsubscribe` and `List-Unsubscribe-Post` present. Gmail's native control uses one-click POST, so it is prefetch-safe, and its presence improves inbox placement. |
| S8-17 | Honeypot | Submit with the hidden field filled. | Silently rejected; no row written. |
| S8-18 | Deployed and always-on | Load the Vercel URL with everything of yours powered off. | Page loads in ~1s. Signup works end to end. A signup link must work when nothing of the owner's is running. |
| S8-19 | Retired code | Grep `app.py`. | No `JOBS`, `JOBS_LOCK`, `run_build`, `parse_interests`, `/status`, `/done`, `MAX_INTERESTS`, `MIN_CHIPS_WITHOUT_TEXT`, or `CHIPS`. |

**Gate:** signup returns immediately and stores exactly the ticked topics; a pending subscriber receives nothing until they confirm; `curl`-ing the unsubscribe URL does not unsubscribe anyone.

---

## Step 9: Orchestration and end-to-end

| ID | Test | Action | Expected result |
|---|---|---|---|
| S9-01 | Stage order | Instrument one run. | `fetch → tag → curate → send`. |
| S9-02 | Successful clock | Complete a run. | `run.status='ok'` and `finished_at` set, only after the final stage succeeds. |
| S9-03 | **Pipeline failure halts** | Force fetch, tag or curate to fail. | `status='failed'`, **the clock does not advance**, and no email is sent. Stages 1–3 are all-or-nothing. |
| S9-04 | **Partial run advances the clock** | Fail one recipient's delivery; let the rest succeed. | `status='partial'`, and **the clock does advance** — fetch, tag and curate covered the window, so rewinding would re-tag paid-for work. The failed recipient retries through `sent`, not by replaying the pipeline. |
| S9-05 | Failure taxonomy is distinct | Compare a stage failure against a delivery failure. | `failed` and `partial` are distinguishable in `run.status` and in `runs.jsonl`. Collapsing them either strands episodes or re-tags everything. |
| S9-14 | Monthly seed scheduled | Inspect `.github/workflows/seed.yml`. | Fires `universe.py --seed-global` on `cron: '0 2 1 * *'`. PRD R1 requires a monthly rebuild, so it must be a job, not a note — an ageing universe presents as declining pick quality and sends you to debug the tagger. |
| S9-15 | Seed does not run inline | Inspect `run.py`. | The every-other-day run does **not** re-seed. Seeding is expensive, uses the same token budget as tagging, and belongs on its own schedule. |
| S9-05 | Idempotent rerun | Run twice back to back. | The second sends nothing and writes no new `sent` rows. |
| S9-06 | Recovery | Fail a run, then run again. | The second re-covers the same window and completes; no episodes lost to the gap. |
| S9-07 | Observability | Complete a run. | One `runs.jsonl` line with `run_id`, `fetch_cutoff_at`, `shows`, `fetched`, `tagged`, `untagged_left`, `tag_abandoned`, `tokens_used`, `score_p50`, `score_p90`, `picks_by_topic`, `subscribers`, `emails_sent`, `emails_failed`, `status`, all matching reality. |
| S9-08 | Backlog signal | Leave episodes untagged across two runs. | `untagged_left` rises visibly rather than the backlog compounding unseen. |
| S9-09 | Skip flags | Run with `--skip-fetch` and `--skip-tag`. | Later stages run against existing data; no API calls made by skipped stages. |
| S9-10 | Workflow schedule | Inspect `.github/workflows/run.yml`. | `cron: '30 1 * * 0,1,3,5'` — **UTC**, which is 07:00 IST on Sun/Mon/Wed/Fri. Verify the weekday shift across the date boundary; the right clock time on the wrong days is the easy mistake. |
| S9-11 | Manual trigger | Fire `workflow_dispatch` from the Actions tab. | Runs once and completes. This is also how you test without waiting for a Sunday. |
| S9-16 | **Runs with the laptop shut** | Close the laptop. Wait for a scheduled run, or trigger one from the GitHub web UI on a phone. | **A real email arrives.** This is the entire requirement — the product operates without its author — and no local test can demonstrate it. |
| S9-17 | Secrets not in code | Grep the repo for key material; inspect workflow logs. | All six credentials come from repository secrets. No secret appears in the code, in `runs.jsonl`, or in Actions log output. |
| S9-18 | Logs survive the runner | Complete a run, then a failing run. | `logs/` is uploaded as an artifact **in both cases** (`if: always()`). On an ephemeral VM they are otherwise destroyed, and `rank.log` is what you need most when tags look wrong. |
| S9-19 | Ephemeral runner holds no state | Run twice. Inspect the runner filesystem between runs. | No `podcaster.db` on disk. All state is in Turso. A local file that appears to work will silently reset every run. |
| S9-20 | **Gap detection** | Skip a scheduled run entirely (disable the workflow for a week). | The absence is detectable. `runs.jsonl` shows a gap; no error exists anywhere because nothing executed. Alerting on failures alone would never catch this, and it is the most likely way the product dies. |
| S9-12 | Clean end-to-end | Three subscribers with overlapping topics, live services, clean `sent`. | Each receives correct, deduped, non-empty email, or nothing if their topics were quiet. |
| S9-13 | **Product acceptance (manual)** | Read the clean-run email as a listener. | The owner would genuinely play at least one recommended episode. If not, Step 5 fails and must be tuned before release. |

**Gate:** a `workflow_dispatch` trigger completes a clean, idempotent, multi-subscriber run, emits correct metrics, and produces episodes the owner would actually play.

---

## Step 10: Documentation

| ID | Test | Action | Expected result |
|---|---|---|---|
| S10-01 | Location | List `doc/`. | `ARCHITECTURE.md`, `podcaster-prd.md`, `IMPLEMENTATION-PLAN.md`, `test-cases.md`. |
| S10-02 | History preserved | `git log --follow doc/ARCHITECTURE.md`. | History traces through the move. |
| S10-03 | **Section numbers resolve** | For every "ARCHITECTURE section N" in a docstring, open section N. | It is about the subject the docstring claims. Roughly twenty of these exist; renumbering breaks them all silently. |
| S10-04 | No contradictions | Compare table counts, scope, cadence and failure semantics across all four documents. | All agree: nine tables, shared pool, no follows, Mon/Wed/Fri/Sun, `failed` halts and `partial` advances. |
| S10-05 | Stale claims in code | `grep -rn 'six.table\|200 shows\|free text' --include='*.py' .` | No hits. **Scoped to executable code** — `doc/` deliberately discusses free text and the six-table schema when explaining what changed and why, so an unscoped grep can never pass and would punish the documentation for doing its job. |
| S10-06 | Stale claims in prose | Read each doc for assertions about the *current* system. | Retracted claims are explicitly marked as retracted (v2's "free text is worth ten times the chip", the 5,000-show target) rather than deleted. A claim that silently disappears reads as an oversight a month later. |
| S10-08 | Target vs present tense | Read the top of `ARCHITECTURE.md`. | A status banner states it describes the target design and that `block-5-tag/` and `block-7-run/` do not exist yet. Without it the file tree reads as documentation drift. |
| S10-06 | Measured findings kept | Read `block-2-universe/README.md`. | The catalogue measurements (~70% stale, near-zero term overlap, 1,641 → 460 usable) survive the rewrite. |
| S10-07 | Vestigial code | Grep for `feedparser` and `LLM_PROVIDER`. | Gone from `requirements.txt` and `.env`. |

**Gate:** no document contradicts another, and every cited section number still resolves.
