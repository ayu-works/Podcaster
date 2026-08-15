# Podcaster — Quick Test Cases

## Scope and assumptions

These tests follow the seven blocks in `IMPLEMENTATION-PLAN.md`, using `ARCHITECTURE.md` for the expected technical behavior and `podcaster-prd.md` for product-quality expectations.

The implementation plan and architecture currently describe a discovery-only version. The PRD additionally asks for followed shows, format preferences, an eight-slot weekly budget, two email sections, feedback links, and quiet-day emails. Those PRD-only features are listed under **Coverage gaps** and are not treated as requirements of the seven current implementation blocks.

Use a dedicated test email address and test database where possible. Mock Podcast Index, Groq, and Resend for repeatable failure-path tests; use the real services for the manual quality gates.

---

## Block 1: Setup

| ID | Test | Action | Expected result |
|---|---|---|---|
| B1-01 | Dependencies import | Activate the virtual environment and import `httpx`, `feedparser`, `groq`, `jinja2`, `resend`, `flask`, and `dotenv`. | Every import succeeds without an exception. |
| B1-02 | Required configuration | Start with all four required environment variables set, then load `config.py`. | Configuration loads and no secret is printed or logged. |
| B1-03 | Missing secret | Remove one required environment variable and run the Block 1 check. | The missing variable and affected block are reported clearly. Block 1 may pass, but the affected block must fail fast when it starts. |
| B1-04 | Config defaults | Inspect the exported constants. | Bar `70`, picks `2`, universe `200`, terms `18`, min description `100`, truncation `400`, max lookback `5`, workers `15`. |
| B1-05 | Database initialization | Run `init_db()` on a new database twice. | Both runs succeed; exactly six tables exist: `user`, `interest`, `candidate_show`, `episode`, `digest`, and `digest_item`. |
| B1-06 | Database constraints | Insert the same episode GUID twice. | The duplicate is rejected or ignored and exactly one episode remains. Upsert behavior is tested in Block 4. |

**Block gate:** `sqlite3 podcaster.db ".tables"` lists all six tables.

---

## Block 2: Podcast Index client and universe

| ID | Test | Action | Expected result |
|---|---|---|---|
| B2-01 | API authentication | Freeze the Unix timestamp and call either Podcast Index function through a mock server. | Headers contain the API key, timestamp, and SHA1 of `key + secret + timestamp`; secrets are not placed in the URL. |
| B2-02 | Show search mapping | Mock `/search/byterm` with valid results and call `search_shows(term)`. | Results are parsed into usable feed IDs, feed URLs, names, and recency fields. |
| B2-03 | Feed episode mapping | Mock `/episodes/byfeedid` with valid episodes and call `episodes_by_feed(feed_id)`. | Episode fields needed by `episode` are returned correctly, including GUID and publish time. |
| B2-04 | API failure | Return a timeout, `429`, and `500` from the mock API. | The client fails visibly or retries according to policy; it does not report an empty successful universe. |
| B2-05 | Interest expansion | Expand two specific interests with a mocked Groq response. | Each interest yields 18 non-empty search terms and includes useful adjacent vocabulary, not only copies of the input. |
| B2-06 | Feed deduplication | Return the same feed from several search terms. | Only one `candidate_show` row is saved for that `feed_id` and user. |
| B2-07 | Recency filter | Return feeds last published 59, 60, and 61 days ago. | Feeds inside the defined 60-day boundary are retained consistently; older feeds are dropped. |
| B2-08 | Universe cap and isolation | Build more than 200 unique current feeds for two users. | Each user has at most 200 rows, and one user's rows never replace or leak into the other's. |
| B2-09 | Rebuild behavior | Build the universe twice for the same user with overlapping results. | No duplicates are created; the final set follows the documented replace/update behavior. |
| B2-10 | Manual relevance gate | Run against the real API using the owner's actual interests and review all saved show names. | The list is predominantly relevant and specific enough to support good episode discovery; generic/off-topic results trigger interest or expansion changes. |

**Block gate:** the target user has 200 unique, recently active candidate shows, and the list passes a manual relevance review.

---

## Block 3: Onboarding page

| ID | Test | Action | Expected result |
|---|---|---|---|
| B3-01 | Render onboarding | `GET /`. | Returns `200` and displays interest chips, free-text refinement, and an email field. |
| B3-02 | Valid subscription | Submit a valid email, at least three chips, and specific free text. | One user is created, all submitted interests are associated with that user, universe setup starts, and a confirmation/setup state is shown. |
| B3-03 | Minimum chips | Submit fewer than three chips. | The form shows a validation error and creates no user or interests. |
| B3-04 | Invalid input | Submit an invalid/blank email or blank required refinement. | The form rejects it safely and creates no partial database records. |
| B3-05 | Duplicate email | Submit the same email twice. | Behavior is deterministic—update/reuse or reject—and does not create two active user records accidentally. |
| B3-06 | Slow universe build | Make `universe.build()` take 30 seconds. | The user sees an immediate setup/loading state or the work runs in the background; no blank page appears. |
| B3-07 | Universe build failure | Force the build to fail after form submission. | The failure is logged and surfaced/recoverable; the UI does not falsely say setup is complete. |
| B3-08 | Stored result | Complete a real self-subscription. | Database contains one user, the submitted interests, and 200 candidate shows belonging to that user. |

**Block gate:** a real form submission completes without a blank wait and produces the expected user, interests, and 200-show universe.

---

## Block 4: Fetch and filter

| ID | Test | Action | Expected result |
|---|---|---|---|
| B4-01 | Incremental window | Set `last_run_at` to two days ago and include episodes just before and after it. | Only episodes newer than the effective boundary enter the candidate pool. |
| B4-02 | Lookback cap | Set `last_run_at` to ten days ago with a five-day maximum. | Fetch begins no earlier than five days ago, preventing a backlog flood. |
| B4-03 | Missed-run recovery | Set `last_run_at` to three days ago, simulating a missed scheduled run. | Episodes from the entire three-day gap are eligible; none are lost due to a hardcoded two-day window. |
| B4-04 | GUID upsert | Return the same GUID from repeated calls with an edited title. | One episode row remains and is updated/ignored according to the upsert policy. |
| B4-05 | Already-sent exclusion | Put an episode in a prior `digest_item` for the user and fetch it again. | It is removed before ranking for that user, while remaining eligible for another user who has not received it. |
| B4-06 | Short description | Test descriptions of 99 and 100 characters. | The 99-character item is dropped; the 100-character item is retained. |
| B4-07 | Trailer and duration | Test a trailer, a 179-second normal episode, and a 180-second normal episode. | Trailer and 179-second episode are dropped; the 180-second non-trailer is retained. |
| B4-08 | Candidate dedupe | Supply duplicate GUIDs within one run. | Only one copy is passed to the ranker. |
| B4-09 | Partial feed failure | Make some of the 200 feed requests time out while others succeed. | Successful feeds still produce candidates, failures are logged, and the run does not silently appear fully successful. |
| B4-10 | Concurrency bound | Instrument simultaneous feed requests. | Peak concurrency is no greater than `FEED_WORKERS` and reaches parallel execution when enough feeds exist. |
| B4-11 | Pool-size diagnostic | Run a real fetch and print/log `raw -> after filter`. | Counts are accurate. At least 60 survive; fewer than 30 blocks progression and triggers universe widening. |

**Block gate:** a live run has 60+ filtered episodes where possible, never includes an already-sent GUID, and clearly reports both raw and filtered counts.

---

## Block 5: Ranker

| ID | Test | Action | Expected result |
|---|---|---|---|
| B5-01 | Prompt contents | Build a prompt from known interests and episodes. | It contains the interests verbatim, stable episode IDs, show/title/duration, and descriptions truncated to 400 characters. |
| B5-02 | Valid response | Return valid JSON with two eligible picks. | IDs, integer scores, and reasons are parsed and returned; no surrounding prose is required. |
| B5-03 | Score gate | Return picks scoring 69, 70, and 90. | The 69 pick is excluded; scores 70 and 90 remain eligible, subject to the two-pick cap. |
| B5-04 | Pick cap | Return three qualifying picks. | At most `PICKS_PER_EMAIL` (2) are returned. |
| B5-05 | Unknown/duplicate IDs | Return an ID absent from the candidates and the same valid ID twice. | Invalid and duplicate entries are rejected; no fabricated episode reaches delivery. |
| B5-06 | Malformed JSON recovery | First response is malformed; retry response is valid. | Exactly one retry occurs with the parse error included, then valid picks are returned. |
| B5-07 | Repeated parse failure | Both responses are malformed. | Ranker returns an empty list without crashing the whole run. |
| B5-08 | Empty is valid | Groq returns `{"picks":[]}`. | The empty result is accepted and proceeds to quiet-day handling. |
| B5-09 | Prompt/response logging | Run one successful and one malformed ranking request. | Full prompts and raw responses are appended to a log with enough context to diagnose the run; API secrets are absent. |
| B5-10 | Specific reason quality | Use a fixture whose description names a guest, claim, or case study, then inspect the reason. | The reason refers to a real detail in that description and avoids generic praise. |
| B5-11 | Three-run manual gate | Rank three live candidate pools and read every reason and source description. | Every accepted reason is concrete and grounded, and each chosen episode is one the owner would plausibly play. Generic reasons block progression. |

**Block gate:** three live runs produce grounded reasons and worthwhile picks; malformed model output produces a quiet result, not a crash.

---

## Block 6: Email

| ID | Test | Action | Expected result |
|---|---|---|---|
| B6-01 | Digest rendering | Render two known picks. | Each item shows title, show, human-readable duration, grounded reason, and listen link. |
| B6-02 | Email constraints | Inspect rendered HTML. | Layout is single-column with a 600px maximum, CSS is inline, there are no external images, and output is below Gmail's 102KB clipping threshold. |
| B6-03 | HTML escaping | Render titles/descriptions containing `<`, `&`, and quotes. | Content is escaped; it cannot inject markup or break the email. |
| B6-04 | Resend request | Mock Resend and send a populated digest. | Sender is `Podcaster <onboarding@resend.dev>`, recipient is the user's email, subject is correct, and rendered HTML is passed once. |
| B6-05 | Pre-send persistence | Pause immediately before the Resend call. | The digest and its item rows already exist, so a send failure is traceable. |
| B6-06 | Successful send | Make Resend return success. | Digest is marked/recorded as sent once, and its items prevent future re-sends. |
| B6-07 | Send failure | Make Resend time out or return an error. | The failure remains distinguishable from a sent digest and is logged; the system does not falsely advance successful-delivery state. |
| B6-08 | Quiet run | Pass zero qualifying picks. | A `quiet` digest is recorded and no Resend request is made, matching the implementation plan. |
| B6-09 | Mobile manual check | Send a real email and open it in a phone email client. | It is readable without horizontal scrolling, links are tappable, and the message is scannable in under 45 seconds. |

**Block gate:** one real two-item email looks correct on a phone, while an empty result records `quiet` and sends nothing.

---

## Block 7: Schedule and end-to-end

| ID | Test | Action | Expected result |
|---|---|---|---|
| B7-01 | Pipeline order | Instrument one run. | Stages execute `fetch -> filter -> rank -> send -> update last_run_at`. |
| B7-02 | Successful run timestamp | Complete a successful sent or quiet run. | `last_run_at` advances only after the cycle reaches its successful terminal state. |
| B7-03 | Failed run timestamp | Force fetch, rank, or send to fail unexpectedly. | `last_run_at` does not advance past unprocessed time, allowing the next run to recover missed episodes. |
| B7-04 | Idempotent rerun | Run the full cycle twice against the same source episodes. | The second run never sends the same episode to the same user. |
| B7-05 | User isolation | Run two users with overlapping shows and different interests/history. | Each receives only their own ranked results; one user's digest history does not suppress the other's episodes. |
| B7-06 | Observability line | Complete a sent and a quiet run. | Each writes one structured summary with `candidates`, `after_filter`, `cleared_bar`, `sent`, and `kind`, and the values match the actual run. |
| B7-07 | launchd configuration | Inspect the plist. | It invokes the correct interpreter and absolute `run.py` path at 07:00 on Sunday, Monday, Wednesday, and Friday. |
| B7-08 | Wake behavior/manual trigger | Load the agent and trigger it manually with `launchctl start`. | The job runs once, writes logs, and does not depend on an interactive shell or working directory. |
| B7-09 | Clean end-to-end run | Clear only test `digest` and `digest_item` rows, then run the cycle using live services. | At most two new, unsent, qualifying episodes are emailed, or a quiet digest is recorded. |
| B7-10 | Product acceptance | Read the clean-run email as a listener. | The owner would genuinely play the recommended episode(s). If not, Block 5 fails and must be tuned before release. |

**Block gate:** a manual launchd trigger completes a clean, idempotent run, emits correct metrics, and produces recommendations the owner would actually play.

---

## Coverage gaps between the PRD and implementation plan

Before treating the full PRD as implemented, add blocks and tests for:

- seeded and promoted `followed_show` records;
- onboarding format preferences and the optional 3–5 followed-show names;
- followed-versus-discovery candidate sourcing and the two-section email;
- the protected discovery slot and no two picks from the same show;
- the three-per-run cap, eight-slot weekly budget, and carry capped at two;
- quiet-day note behavior, including at most one quiet email per week;
- signed thumbs-up/down links, replay/tamper protection, and promotion on thumbs-up;
- timezone-aware 07:00 scheduling; and
- PRD success-metric tracking.

Also resolve these document conflicts before testing those features:

1. The implementation plan says quiet runs send nothing; the PRD says the first quiet run of a week sends a one-line note.
2. The architecture specifies `launchd`; PRD R6 says cron. For a sleeping MacBook, the current plan correctly chooses `launchd`.
3. The architecture has six tables; the PRD data model requires `format_pref`, `followed_show`, and `feedback` as well.
4. The architecture describes discovery only; the PRD defines followed-show notifications as v1 scope.
