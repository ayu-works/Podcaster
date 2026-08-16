# Podcaster: Architecture

> **Status: target design (v3). Not yet implemented.**
> The repository currently implements the v1 per-user flow described in section 2 under "Constraint two". The file tree in section 4 and the schema in section 5 describe where the code is going, not where it is. `block-5-tag/` and `block-7-run/` do not exist yet.

**Scope:** a shared episode pool, tagged once, filtered by topic. Subscribers tick topics from a fixed list and get up to 10 recommended episodes every other day. Followed-show notifications and any per-user personalisation are out of scope.

> **Section numbers are load-bearing.** About twenty docstrings across the codebase cite this document by number (`config.py` "section 9", `db.py` "section 5", `fetch.py` "section 6", `podcastindex.py` "section 2"). The eleven slots below are stable. Rewrite their contents, never their numbering.

---

## 1. What it does

```
1. Once a month: search Podcast Index across the 20 topics,
   keep a global universe of shows
        |
2. Every other day: poll those shows for new episodes
        |
3. A Groq-hosted model reads each NEW episode once and
   returns topics, a 0-100 score, and a one-line why
        |
4. Per topic, take the top 10 above the bar (pure SQL)
        |
5. Per subscriber, look up the topics they ticked, email them
```

If nothing in a topic clears the quality bar, subscribers to that topic get nothing. Silence is a feature, not a failure.

The critical property: **steps 1 to 4 run once for everyone.** Only step 5 runs per subscriber, and it is a single query. The thousandth subscriber costs one SQL lookup.

---

## 2. The core idea

Everything comes from the **Podcast Index API**: a free, open catalogue of roughly 4 million podcast feeds.

Two constraints shape the entire design.

### Constraint one: the API searches shows, not episodes

> **Podcast Index searches show names, not episode contents.**

There is no way to ask it "find recent episodes about AI agents in production." Episode-level search exists in Taddy and Listen Notes, both behind tighter free tiers. Not worth it at this stage.

Nor is there any way to ask the internet "what podcasts came out today." A podcast is an RSS feed; to know a feed published something you must poll that feed. So a stored list of shows is not overhead, it is a prerequisite.

It is also the **cost control**, and it is bounded by the tagging token budget rather than by taste. See section 9 — the universe size is *derived* from how many tokens a run can afford, not chosen freely.

| When | What happens | Cost |
|---|---|---|
| **Monthly** | Search for *shows* across the 20 topics. Keep `SHOW_TARGET`. | ~10 minutes |
| **Every run** | Poll those shows for new episodes. | ~1 minute |

### Constraint two: the match must be stored, not computed

This is the change from v1, and it is the reason this document exists in its current form.

v1 computed the interest-to-episode match **inside a prompt, at send time, once per user**. The user's free-text interests were pasted into a ranking call alongside candidate episodes, and the model picked two. Nothing about that match was ever written to the database. It existed for a few seconds and was discarded.

That has three consequences, all of which were observed:

1. **It does not scale.** N users meant N × 200 feed polls and N ranking calls per run.
2. **The ranker was structurally blind.** Groq's free tier caps at 8,000 tokens per minute, so the candidate pool was truncated from ~180 episodes to ~25 *before the model saw anything*.
3. **Nothing was queryable.** You could not ask "which episodes are about AI." No table knew.

So the design inverts the question. Instead of asking *"which episodes fit this user?"* once per user, ask *"what is this episode about?"* once per episode, and **write the answer down**:

```
episode_topic:  (episode 1183, "technology-ai")     <- stored, not inferred
```

The interest-to-episode link becomes a row. Sending becomes a join. Every fetched episode gets tagged, so 100% of the pool is reachable instead of 15%.

### The tradeoff, and it fails silently

**The tagger is now the single point of failure.** Every subscriber's email derives from `episode.score` and `episode_topic`. If tagging degrades quietly — scores bunching at 78 to 82, topics collapsing onto two popular slugs — nothing downstream notices, and every subscriber's email gets worse at the same moment.

This replaces v1's named risk (the 200-show per-user ceiling), which this design retires.

Guards:

1. Hand-read 20 tagged rows at build time. See the Step 5 check in the implementation plan. This check must never be cut.
2. Log the score distribution in `runs.jsonl`. A flat distribution means curation is really just recency and the bar is doing nothing.
3. `looks_generic()` rejects unfounded why-lines automatically, diverting them to the retry queue.

A second, accepted tradeoff: two subscribers on the same topic receive the same picks, diverging over time only through the `sent` table. `episode.score` is one number per episode, not per topic-pair. Both are deliberate, and both are what buys the scaling.

### The state discipline

Three pieces of state decide whether this system loses data quietly. They are stated here because all three are easy to get subtly wrong and none of them fail loudly.

**Overlap is safe, gaps are not.** Every boundary in this system is deliberately conservative in the direction of doing work twice. Upserts on `guid` are idempotent, so re-fetching an episode costs nothing; missing one loses it permanently. Whenever a choice exists between a tighter window and a looser one, take the looser one.

**Every irreversible action has an attempt record written before it happens.** Sending an email cannot be undone, so a row describing the attempt is committed first. The row carries a status, not merely an existence — a row that only says "we got this far" cannot distinguish success from failure.

**Anything retried must be reachable when it is retried.** A work item deferred to the next run must still be eligible on the next run. Deferral windows and eligibility windows have to be the same window, or retries silently become deletions.

---

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Matches the GitHub Actions runner image. |
| Storage | **Turso (libSQL)** | Hosted SQLite. Same dialect, so the schema and every query port unchanged. See section 7. |
| Web | Flask | Onboarding is three routes. Do not reach for anything bigger. |
| Catalogue | Podcast Index API | Free, open, ~4M feeds. |
| LLM | `groq` SDK | Search-term expansion (monthly) and tagging (per episode). |
| Email | Resend | Simple API, good deliverability. |
| Schedule | **GitHub Actions** | Cloud cron. Runs without any machine of yours being awake. See section 7. |

Libraries: `httpx`, `groq`, `jinja2`, `resend`, `flask`, `python-dotenv`, `libsql-client`.

**Everything above is on a free tier.** That is a deliberate constraint and it costs the product exactly one thing: `SHOW_TARGET` is capped at ~2,500 shows by Groq's daily token limit (section 9). Nothing else in the design is compromised by it.

---

## 4. Files

**Target layout.** One folder per block. Each module is a standalone CLI run from inside its own folder; `_shared.py` in each folder is an import shim that puts the earlier blocks on `sys.path`.

```
Podcaster/
  .env                    keys for local dev, never committed
  doc/                    this file, the PRD, the plan, the test cases

  .github/workflows/
    run.yml               the every-other-day digest        <- the scheduler
    seed.yml              the monthly universe rebuild

  block-1-setup/
    config.py             all tunable numbers, and TOPICS
    db.py                 schema + connection
    check.py

  block-2-universe/
    podcastindex.py       API client
    universe.py           builds the global show list

  block-3-onboarding/
    app.py                Flask: topic checkboxes, subscribe, unsubscribe
    templates/

  block-4-fetch/
    fetch.py              poll every show for new episodes

  block-5-tag/            <- does not exist yet
    tag.py                one model call per batch of 20  <- the product
    curate.py             top N per topic, pure SQL

  block-6-email/
    email_out.py          render + send, per subscriber
    templates/digest.html

  block-7-run/            <- does not exist yet
    run.py                fetch -> tag -> curate -> send
```

There is no `podcaster.db` in the tree. The database is hosted (section 7) because the thing that runs the pipeline is a fresh, disposable virtual machine that keeps nothing.

Each pipeline stage is a separate file on purpose. When a pick is bad you want to answer "which stage caused this" in ten seconds. That matters more than usual here, because the failure mode is subtle (mediocre picks) rather than loud (a crash).

---

## 5. Database

Nine tables. The shape of the schema *is* the architecture: everything AI-derived lives on `episode`, computed once and read by everyone.

```sql
subscriber                     -- who gets email
  id, email UNIQUE, unsub_token UNIQUE, confirm_token UNIQUE,
  created_at, confirmed_at,
  status                       -- pending | active | paused | unsubscribed
                               -- pending until double opt-in completes;
                               -- only 'active' ever receives a digest

subscription                   -- what they ticked
  subscriber_id, topic         -- topic is a slug from config.TOPIC_SLUGS
  PRIMARY KEY (subscriber_id, topic)

show                           -- the global universe. No user_id.
  id, feed_id UNIQUE, feed_url, title, added_at,
  status                       -- active | muted

show_topic                     -- which topic's terms surfaced this show
  show_id, topic               -- debug/coverage only, never used for matching

episode                        -- one shared pool
  id, guid UNIQUE, feed_id, show_name, title, description,
  duration_sec, published_at, web_url,
  score, why, tagged_at,       -- AI output. tagged_at NULL = not yet tagged
  tag_attempts DEFAULT 0,      -- retry counter, capped
  tag_error                    -- last failure reason, for diagnosis

episode_topic                  -- the stored match
  episode_id, topic

run
  id, started_at, fetch_cutoff_at, finished_at,
  fetched, tagged, emails_sent, emails_failed,
  status                       -- running | ok | partial | failed

daily_pick                     -- the day's editorial output, ~200 rows
  run_id, topic, episode_id, rank

sent                           -- delivery attempts, not just successes
  subscriber_id, episode_id, run_id,
  status,                      -- pending | sent | failed
  attempts DEFAULT 0, last_error, created_at, sent_at
  PRIMARY KEY (subscriber_id, episode_id)
```

**Six invariants that will save you:**

1. **Dedupe on `guid`, never on title.** Feeds republish episodes with edited titles constantly. Title-based dedupe means sending the same episode twice, which is the single thing most likely to make this feel broken.

2. **`fetch_cutoff_at`, not `finished_at`, is the clock.** It is captured at the *start* of a run, immediately before fetch begins, and the next run reads `MAX(fetch_cutoff_at) WHERE status IN ('ok','partial')`.

   Using `finished_at` would be a permanent data-loss bug. A run takes 20 to 30 minutes, almost all of it tagging. An episode published after fetch ran but before the run finished would sit inside the window the next run skips over, and would never be fetched by anything, ever. Capturing the cutoff first means the next run re-covers a few minutes of already-seen episodes, which the `guid` upsert absorbs for free.

3. **`sent` records attempts, not successes.** A row is written with `status='pending'` and committed *before* Resend is called, then updated to `sent` or `failed`.

   The dedupe query excludes `pending` and `sent` but **not** `failed`, so a failed delivery becomes eligible again on the next run. `pending` is treated as already-sent — deliberately conservative, inherited from v1: a crash between the commit and the API call leaves a row that might or might not have been delivered, and sending twice is worse than sending nothing.

4. **`tagged_at` is per episode, and retries are bounded.** A killed tagging run resumes at episode 601 rather than restarting at 1. The work queue is `tagged_at IS NULL AND tag_attempts < TAG_MAX_ATTEMPTS`. Without the attempt cap, an episode whose description reliably produces a generic why-line is retried forever, on every run, at cost.

5. **Curation selects on `tagged_at`, not `published_at`.** See section 6, stage 3. An episode deferred by a tagging retry is tagged on a *later* run than the one that fetched it. If curation filtered on publication time within the current run's window, every retried episode would be tagged successfully and then silently dropped, having missed its only chance to be curated.

6. **`sent` is the only re-send guard,** and it is per subscriber, so an episode one person has seen stays available to everyone else. This is why the already-sent filter moved out of `fetch.py` — a global fetch must not be narrowed by one subscriber's history.

`show` has no `user_id`. That single absence is the difference between v1 and v2.

---

## 6. How a run works

### Stage 1: fetch

**First, write `run.fetch_cutoff_at = now()`.** Before polling anything. This is the boundary the *next* run will start from, and capturing it before the work rather than after is what closes the mid-run publication gap described in invariant 2.

Then, for every active `show`, get episodes published since the previous run's `fetch_cutoff_at`. Run 15 concurrently or this takes minutes instead of seconds.

**Use the last good cutoff, not "the last 2 days."** A fixed window silently drops episodes whenever a run fails or the laptop was asleep. Using the last successful run means a missed Wednesday gets picked up on Friday. Cap the lookback at 5 days so a long gap does not produce a flood.

Upsert into `episode` on `guid`.

Cheap deterministic filters, no LLM: drop descriptions under 100 characters, drop trailers and anything under 3 minutes, dedupe on `guid`. The description rule does the most work — the tagger cannot judge what it cannot read, and thin descriptions are the biggest single source of bad tags.

If *every* feed fails, raise. A zero-candidate run otherwise looks exactly like a quiet week, and now it would look like one for every subscriber simultaneously.

### Stage 2: tag

Batched Groq calls, `TAG_BATCH_SIZE` episodes each. **This is the product, everything else is plumbing.**

```
SYSTEM
You tag podcast episodes for a recommendation service. You are a filter,
not a feed. Your job is to protect listeners' time.

For each episode return:
- topics: 0-3 slugs from the allowed list. Only slugs the episode is
  genuinely ABOUT. An empty array is correct and common - an episode that
  fits nothing is dropped, which is the desired outcome.
- score: 0-100, how much this is worth a listener's time. 70 means "worth
  it". Be harsh; most episodes are not worth most people's time. Prefer
  episodes whose description names a concrete claim, guest, case study or
  number. Vague descriptions score low even when on-topic.
- why: ONE sentence that MUST reference something specific from that
  episode's description - a named guest, a claim, a case study, a number.
  Generic praise ("a great listen", "perfect for anyone interested in X")
  is a failed output, not a weak one.

Allowed topic slugs:
technology-ai, business-startups, design, science, history, finance,
culture, politics, health-fitness, comedy, true-crime, sport,
personal-development, food-cooking, music, film-tv, books-writing,
philosophy, climate-energy, travel

Output ONLY valid JSON:
{"episodes":[{"id":<int>,"topics":["<slug>"],"score":<int>,"why":"<one sentence>"}]}

USER
[1] show: ... | title: ... | 42m | desc: {first 400 chars}
[2] ...
```

Implementation notes:

- Work queue is `tagged_at IS NULL AND tag_attempts < TAG_MAX_ATTEMPTS`. **Increment `tag_attempts` on every attempt**, success or failure, and write `tag_error` on failure.
- Candidates are numbered from 1 rather than carrying database ids. Small integers are cheaper and the model makes fewer transcription mistakes with them.
- Truncate descriptions to `DESC_TRUNCATE` characters. Past that is sponsor reads and link dumps that dilute the signal.
- On malformed JSON, retry once in-call with the error appended, then leave the batch untagged rather than crash. It is retried next run, up to the attempt cap.
- `looks_generic(why, description)` checks a stock-phrase list plus "shares no substantial word with the description." A hit leaves `tagged_at` NULL for retry. Do not fail the run — at scale a handful of weak reasons is normal.
- An episode that exhausts `TAG_MAX_ATTEMPTS` is abandoned, counted, and never retried again. Some descriptions genuinely cannot produce a grounded reason.
- **Rate limiting is real and has two ceilings.** `GROQ_TPM` bounds throughput within a run; `GROQ_TPD` bounds the whole day. Section 9 explains why the second one, not taste, sets the universe size. Pace against TPM, track cumulative spend against TPD, and stop cleanly when the daily budget is exhausted rather than failing mid-batch.
- Commit per batch, never at the end.
- Log every prompt and response to a file. When tags are bad this is how you find out why, and you will read it constantly in week one.

### Stage 3: curate

**No AI.** One SQL query per topic.

**Select on `tagged_at`, not `published_at`:**

```sql
WHERE t.topic = :topic
  AND e.score >= :bar
  AND e.tagged_at > :previous_cutoff        -- newly tagged, whenever published
  AND e.published_at > :staleness_floor     -- but not ancient
ORDER BY e.score DESC, e.published_at DESC
```

This is invariant 5, and it is the whole reason the query looks like this. Tagging retries mean an episode fetched on Monday may not be tagged until Wednesday. Filtering on publication date inside the current run's fetch window would let that episode be tagged successfully and then dropped before it ever reached a list — a silent deletion that no counter would report. Selecting on when it *became eligible* rather than when it was *published* gives every episode exactly one shot at curation, on the run that finished tagging it.

`:staleness_floor` is `now() - CURATE_MAX_AGE_DAYS` and exists only so a long tagging backlog cannot surface three-week-old episodes as today's picks.

Then, in Python: keep at most `CURATE_MAX_PER_SHOW` per feed, take the top `PICKS_PER_TOPIC`, write to `daily_pick`.

Twenty queries produce the entire day's editorial output.

A topic with nothing above the bar writes **zero rows**. That is "if nothing's good, it sends you nothing", expressed as a `WHERE` clause. Do not backfill.

The per-show cap and the bar are enforced **in code, not in the prompt**. A prompt instruction is a request; a post-filter is a guarantee.

### Stage 4: deliver

Per active subscriber: join their `subscription` topics to this run's `daily_pick`, drop anything in `sent` with status `pending` or `sent`, dedupe by episode (one episode can appear under two of their topics), apply `MAX_PER_SHOW_PER_EMAIL`, group by topic for display, cap at `MAX_PER_EMAIL`.

**`MAX_PER_SHOW_PER_EMAIL` is a separate cap from `CURATE_MAX_PER_SHOW` and both are needed.** Curation limits a show to 2 episodes *per topic list*; without a second cap at send time, a subscriber to four topics could receive eight episodes from the same show and the promise in the PRD would be false.

No qualifying picks means no email. Never an empty send.

**Delivery is per-recipient isolated.** One subscriber's send failing does not stop the others — see section 7 for how that interacts with run status. Per recipient:

```
1. INSERT sent rows with status='pending', increment attempts   -- commit
2. call Resend
3. UPDATE those rows to 'sent' (with sent_at) or 'failed' (with last_error)
```

Step 1 commits before step 2 because sending is irreversible and a crash between them must be recoverable. A `failed` row leaves those episodes eligible for the next run; a `pending` row does not, because a crash after the API call cannot be distinguished from one before it, and a duplicate email is worse than a missed one.

---

## 7. Scheduling and hosting

**The pipeline runs on GitHub Actions. Nothing runs on a laptop.**

This reverses v1's guidance, and the reversal is the point. v1 argued for macOS `launchd` over `cron` because a MacBook sleeps and cron silently skips jobs scheduled during sleep. That reasoning was correct for a personal tool, where the worst case is *your own* digest arriving late.

It is the wrong answer for subscribers. A laptop that is shut, travelling, or offline produces no run at all — every subscriber gets nothing, and none of them is told why. "launchd fires the missed job on wake" means a 07:00 send lands at 4pm when you reopen the lid. For you that is a shrug; for someone who subscribed, it is a product that does not work.

So the requirement is not "on a scheduler" but **"on a scheduler that is always on and is not yours."**

| Workflow | Schedule (UTC) | Runs |
|---|---|---|
| `.github/workflows/run.yml` | `30 1 * * 0,1,3,5` | `run.py` — fetch, tag, curate, send |
| `.github/workflows/seed.yml` | `0 2 1 * *` | `universe.py --seed-global` |

Both also carry `workflow_dispatch` so they can be triggered by hand from the Actions tab — which is also how you test them without waiting for a Sunday.

**Cron in Actions is UTC.** 07:00 Asia/Kolkata is `01:30` UTC, and that offset crosses a date boundary, so the weekday list is shifted accordingly. Get this wrong and the digest goes out on the right clock time on the wrong days.

The monthly seed is a real scheduled job, not a note in a document. Without it the universe ages: shows go dead, new ones never enter, and coverage decays in a way that presents as a slow decline in pick quality rather than as a maintenance failure — so you will debug the tagger instead.

### Why Actions, specifically

A 20-to-30-minute Python batch job is an awkward shape for most free platforms. Cloudflare Workers and similar edge runtimes cap execution far below it; serverless function platforms generally do too. Actions allows six hours per job, which is not close to a constraint here.

Budget: roughly 30 minutes × 4 runs ≈ **120 minutes a month**, against 2,000 free on a private repository and unlimited on a public one. The tagging stage is mostly *sleeping* against the Groq rate limit rather than computing, but Actions bills wall clock, so that idle time is the bill.

Secrets — `PODCASTINDEX_KEY`, `PODCASTINDEX_SECRET`, `GROQ_API_KEY`, `RESEND_API_KEY`, `DATABASE_URL`, `DATABASE_TOKEN` — live in repository secrets. `.env` is for local development only and is never committed.

### Two Actions-specific failure modes

Both are silent, which puts them in the same family as everything else this document warns about.

1. **Scheduled workflows drift under load.** A run set for 01:30 UTC can start 10 to 30 minutes late. Acceptable for a morning digest; do not design anything that assumes precise timing.

2. **GitHub disables scheduled workflows after roughly 60 days with no repository activity.** If you stop committing, the digest stops, no email is sent, and nothing announces it. This is the single most likely way this product dies quietly, and it is not a code failure — no log will contain it, because no run will occur. Guard by watching for a *gap* in `runs.jsonl` rather than for an error inside it, and confirm the current policy in GitHub's documentation rather than trusting this paragraph.

### Where the database lives

**Turso (libSQL), hosted, free tier.**

The forcing constraint is that an Actions runner is **ephemeral**: every run gets a fresh virtual machine that is destroyed when the job ends. A local `podcaster.db` would not survive a single run. It also cannot live on your laptop, because the signup page has to write to the same database the pipeline reads.

Turso is chosen over Postgres for one reason: **libSQL is SQLite**, so the dialect is identical. `datetime('now')`, `INSERT … ON CONFLICT`, the partial index on `episode(tagged_at)` — all port unchanged. The only code that moves is `db.connect()`, which reads `DATABASE_URL` and `DATABASE_TOKEN` from the environment instead of opening a file. `session()`, `init_db()`, and every query in blocks 2 through 7 are untouched.

Neon or Supabase (both Postgres, both free) are the conservative alternatives — more established free tiers, at the cost of an afternoon porting dialect: `datetime('now')` → `now()`, `INTEGER PRIMARY KEY` → identity columns, different partial-index syntax, different upsert details. Worth it only if Turso's terms change or its newness is a concern.

WAL and the single-writer assumption stop being local implementation details once the database is remote and two clients exist — the pipeline and the signup app. The write patterns do not overlap in practice (signups are single-row inserts; the pipeline is a batch), but do not assume that forever.

### The signup app

**Vercel, Python serverless functions, free Hobby tier.** `api/index.py` exports the Flask `app`; the repo's existing `_shared.py` shim pattern puts `block-1-setup/` on the path so `config` and `db` are imported unchanged.

Turso is reached over its **HTTP API**, not a persistent connection. This matters: a serverless function is spun up per request and cannot hold a connection pool. libSQL's HTTP mode is stateless per query, which is exactly the shape serverless needs.

**Why not Render**, which is the obvious "just deploy Flask" answer: free instances spin down after inactivity and cold-start in roughly **50 seconds**. For a batch job that is irrelevant; for a signup link someone taps from a tweet it is fatal — they leave before the page paints. Vercel's cold start is about a second. Check both platforms' current terms before committing, but the spin-down asymmetry is the deciding factor and is unlikely to invert.

Not Cloudflare Workers, despite being the best free edge platform, because it would mean writing these three routes in JavaScript and carrying two languages for the sake of a form. Not PythonAnywhere: its free tier restricts outbound network access to a whitelist, which blocks Turso.

### Two problems a public form creates that a personal tool did not

Both are live the moment the signup URL is shareable, and neither existed when the only user was the author.

**1. Anyone can subscribe anyone.** An unauthenticated `POST /subscribe` that immediately starts sending email is a mechanism for mailing a stranger without their consent. Beyond being wrong, it is a deliverability risk: recipients who never signed up mark it spam, and enough of that poisons the sending domain for every legitimate subscriber.

The fix is **double opt-in**. New rows land as `status='pending'`, a confirmation email goes out, and clicking its link flips the row to `active`. The send stage already filters on `active`, so pending rows are inert — a hostile signup costs one confirmation email and nothing else. `subscriber.status` therefore carries four values: `pending`, `active`, `paused`, `unsubscribed`.

**2. Link scanners will unsubscribe people.** Gmail, Outlook and corporate security gateways **prefetch URLs in email** to check them for malware. A `GET /unsubscribe/<token>` that acts on retrieval will be triggered by a scanner that no human asked, and subscribers will silently vanish with no record of why.

So `GET` must be **safe**: it renders a confirmation page with a button. The button issues a `POST`, and only the `POST` mutates. This is the ordinary HTTP contract — `GET` does not change state — and ignoring it is a common and genuinely damaging bug.

Also send the `List-Unsubscribe` and `List-Unsubscribe-Post` headers, so Gmail's own unsubscribe control appears and works. That control uses one-click POST, which is why it is safe from prefetch, and its presence measurably improves inbox placement.

**Abuse control** beyond that stays light: a hidden honeypot field that real users never fill, and a per-IP throttle on `POST /subscribe`. Both are cheap; neither is worth more effort until abuse actually appears.

### Failure semantics

Stages have different failure rules, and conflating them is how half-built digests get sent.

**Stages 1 to 3 are all-or-nothing.** A failure in fetch, tag or curate stops the run. `status='failed'`, the clock does not advance, and the next run re-covers the same window.

**Stage 4 is per-recipient isolated.** One subscriber's delivery failing does not abort the others. If any recipient fails, the run ends `status='partial'`.

**A `partial` run still advances the clock.** Fetch, tag and curate all succeeded, so re-covering the window would re-tag work already done. Failed recipients are retried through their own `sent` rows with `status='failed'`, not by rewinding the pipeline. This is why invariant 2 reads `status IN ('ok','partial')`.

The "since last successful cutoff" window in Stage 1 is what makes a missed run harmless rather than lossy.

---

## 8. Onboarding page

Three routes in `app.py`.

**`GET /`** renders the form: the 20 topics from `config.TOPICS` as checkboxes, pick at least one, plus an email field.

**There is no free-text box, and this is a design decision rather than a simplification.** In v1 free text was the real signal, because it was pasted into the ranking prompt word for word. In v3 there is no prompt at send time — matching is `WHERE topic IN (...)`. An interest that is not a slug has nothing to join against. Supporting free text would require embeddings, which reintroduces per-user work at send time and defeats the entire redesign.

The evidence agrees. v1's only real user typed *"Technology & AI — anything in it"* and *"Business & Startups — just live it"*: chip labels with a shrug appended. The free-text field was not being used as designed even by its author.

**If a topic feels too coarse, add slugs — do not add a text box.** Split `technology-ai` into `ai-engineering` / `ai-research` / `ai-business`. Topics cost a little prompt length and some seeding. A text box costs the architecture.

There is also no personality or archetype quiz, for the same reason as v1: a quiz produces a label that then has to be decoded back into topics.

**`POST /subscribe`** creates the subscriber with `status='pending'`, two `secrets.token_urlsafe(32)` tokens (confirm and unsubscribe), writes `subscription` rows, sends the confirmation email, renders "check your inbox". Synchronous and instant — nothing is built at signup any more, so v1's 30-second universe build, background thread, in-memory job registry and status-polling page are all gone.

**`GET /confirm/<token>`** flips `pending` to `active` and stamps `confirmed_at`. Until this happens the subscriber receives nothing, because the send stage filters on `active`. Idempotent.

**`GET /unsubscribe/<token>`** renders a confirmation page with a button and **changes nothing**. **`POST /unsubscribe/<token>`** performs it. Email link scanners prefetch URLs, so a `GET` that mutates would unsubscribe people no human ever asked to remove — see section 7. Both are idempotent: an unknown or already-used token still renders success, never a 500 and never a hint about whether the token was real.

---

## 9. Config

Every tunable in `config.py`, nothing buried in logic:

```python
RELEVANCE_BAR          = 70      # the most important number here
PICKS_PER_TOPIC        = 10      # rows in daily_pick per topic per run
MAX_PER_EMAIL          = 10      # cap after merging a subscriber's topics
MAX_PER_SHOW_PER_EMAIL = 2       # the PRD's promise, enforced at send
CURATE_MAX_PER_SHOW    = 2       # per topic list
CURATE_MAX_AGE_DAYS    = 7       # staleness floor for late-tagged episodes
TERMS_PER_INTEREST     = 18      # measured minimum for a fresh universe
TAG_BATCH_SIZE         = 20      # episodes per tagging call
TAG_MAX_TOPICS         = 3       # topics one episode may carry
TAG_MAX_ATTEMPTS       = 3       # then abandon; some descriptions never work
MIN_DESC_CHARS         = 100
DESC_TRUNCATE          = 400
MAX_LOOKBACK_DAYS      = 5
FEED_WORKERS           = 15

GROQ_TPM               = 8_000   # throughput ceiling within a run
GROQ_TPD               = 200_000 # daily ceiling - this is the binding one
SHOW_TARGET            = 2_500   # DERIVED, see below. Do not raise casually.
```

`TOPICS` also lives here, as `(slug, label)` pairs. It is the single source for show seeding, the tagging prompt's allowed labels, curation, and the onboarding checkboxes. It must not be duplicated anywhere else.

### `SHOW_TARGET` is derived, not chosen

This is the least obvious constraint in the system and the one most likely to be raised without thinking.

Shows publish roughly every 3 to 4 days on average, so a 2-day run sees new episodes from something like a third of the universe. Each episode costs roughly 180 tokens of input (a 400-character description plus title and show name) and ~40 of output, and each batch of 20 re-sends the ~400-token system prompt. That works out to about **220 tokens per episode, all in**.

Against `GROQ_TPD = 200_000`, and reserving headroom for retries and the monthly seed:

```
usable daily budget    ~170,000 tokens
tokens per episode     ~220
episodes per run       ~770
universe size          ~770 x 3   =  ~2,300 shows
```

Hence 2,500, and hence not 5,000. An earlier draft of this document specified 5,000 shows on the basis of TPM alone; that run would have needed ~308,000 tokens and could not have completed in a single day, let alone four times a week.

**The free tier is the binding constraint on the whole product's reach.** The paid path is the obvious fix and it is cheap: a Groq developer tier raises `GROQ_TPD` by orders of magnitude, at which point `SHOW_TARGET` can rise to 5,000+ and the arithmetic above is the only thing that needs redoing. Nothing else in the design changes.

Levers if staying on free tier and more coverage is wanted, in order of preference:

1. Lower `DESC_TRUNCATE` to 300 — roughly a 20% token cut for a modest quality cost.
2. Raise `TAG_BATCH_SIZE` to 40 — halves system-prompt overhead, ~10% saving.
3. Drop to 3 runs a week instead of 4.

`RELEVANCE_BAR` deserves attention. It alone decides whether this feels curated or spammy, and 70 is a guess. Expect to move it in week one. Keeping it here, rather than baked into the prompt, is why calibrating takes seconds instead of a rewrite.

`PICKS_PER_TOPIC = 10` is the specified design, and is worth watching. Ten items is closer to a feed than a filter, and the product's own pitch opens by complaining about volume. It is one constant.

---

## 10. Observability

The failure mode is silent, so log one line per run to `logs/runs.jsonl`:

```json
{"run_id":14,"ran_at":"...","fetch_cutoff_at":"...","shows":2480,
 "fetched":812,"tagged":790,"untagged_left":22,"tag_abandoned":3,
 "tokens_used":168400,"score_p50":61,"score_p90":79,
 "picks_by_topic":{"technology-ai":10,"travel":0},
 "subscribers":340,"emails_sent":318,"emails_failed":2,"status":"partial"}
```

| Signal | What it means |
|---|---|
| `fetched` very low | Universe too narrow or the API is failing. Looks identical to "a quiet week" from outside, but it is a bug. |
| `untagged_left` growing run over run | Tagging is not keeping up with the rate limit, or the daily budget is being exhausted mid-run. Compounds silently. |
| `tag_abandoned` climbing | Episodes are hitting the attempt cap. A few is normal; a trend means the prompt or `looks_generic` is mis-tuned. |
| `tokens_used` near `GROQ_TPD` | The universe has outgrown the tier. Coverage is being silently truncated — episodes fetched but never tagged. |
| `score_p50` and `score_p90` close together | **The most important signal here.** A flat distribution means the model is not discriminating, curation is really just recency, and the bar is doing nothing. |
| Many topics at 0 picks | Bar too high, or the universe under-covers those topics. Cross-check `show_topic` counts. |
| Every topic at the cap | Bar too low, filler is leaking in. This is the worse direction to be wrong in. |
| `emails_failed` non-zero | Expected occasionally; those episodes retry next run. A trend means a deliverability problem, not a code problem. |
| **A missing line entirely** | **The worst signal, and the only one that is an absence rather than a value.** No line means no run: the workflow was auto-disabled for repository inactivity, the schedule drifted past a skip, or credentials expired. Nothing errored, because nothing executed. Alert on the *gap*, not on the contents. |

---

## 11. Path to scale

| Change | Effort |
|---|---|
| A second real subscriber | **Blocking.** `onboarding@resend.dev` is Resend's sandbox sender and delivers only to the account owner. Verify a domain, add SPF and DKIM, change `FROM_EMAIL`. Nothing else in the code changes. |
| Unsubscribe | **Blocking.** Route plus footer link. Legally required before mailing anyone else. |
| **A universe beyond ~2,500 shows** | **Blocking on the free tier.** `GROQ_TPD` caps it. A paid Groq tier is the fix; redo the section 9 arithmetic with the new number. No code changes. |
| Signup page hosting | **Resolved:** Vercel Python serverless, Turso over HTTP. See section 7. |
| Outgrowing Turso's free tier | Neon or Supabase, at the cost of a Postgres dialect port. Schema and pipeline are unaffected. |
| Per-subscriber send times | Add `timezone` to `subscriber`, group the send stage by offset. Stages 1 to 3 are shared and unaffected. |
| Finer topics | Add slugs to `TOPICS`, re-seed. Existing rows are unaffected; new episodes tag against the wider set. Note this raises tokens per episode slightly, since the slug list is in every prompt. |
| Per-user personalisation | Embed each subscriber's own words, re-sort their already-topic-matched list by similarity. A dot product at send time, never a model call — and only worth it once tag quality is proven. |

### On "SQLite to Postgres is a connection string"

An earlier draft of this document said that, and it is not quite true. The schema ports cleanly, but `datetime('now')` becomes `now()`, `INTEGER PRIMARY KEY` becomes `GENERATED … AS IDENTITY`, the partial index on `episode(tagged_at)` needs different syntax, and `INSERT … ON CONFLICT` differs in detail. Call it an afternoon, not a config change.

Choosing Turso avoids paying that afternoon at all, which is the main reason it is the default here.
