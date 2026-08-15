# Podcaster: Architecture

**Scope:** discovery only. User picks interests, subscribes by email, gets ~2 recommended episodes every other day. Notifications from shows they already follow is v2.

---

## 1. What it does

```
1. User opens a page, picks interests, enters email
        |
2. We find ~200 podcasts that match those interests
        |
3. Every other day: check those 200 for new episodes,
   A Groq-hosted model picks the best 2
        |
4. Email them, with one line on why each
```

If nothing clears the quality bar, send nothing. Silence is a feature, not a failure.

---

## 2. The core idea

Everything comes from the **Podcast Index API**: a free, open catalogue of roughly 4 million podcast feeds.

It has one limitation that shapes the entire design:

> **Podcast Index searches show names, not episode contents.**

There is no way to ask it "find recent episodes about AI agents in production." Episode-level search exists in Taddy and Listen Notes, both behind tighter free tiers. Not worth it at this stage.

So the design works around it:

| When | What happens | Cost |
|---|---|---|
| **Once, at signup** | Search for *shows* matching the user's interests. Keep the top 200. | ~30 seconds |
| **Every run after** | Just check those 200 for new episodes. | ~10 seconds |

Do the expensive matching once, at the show level, where the API is strong. Then check a short list forever.

**Why this matters:** the naive alternative is sweeping "recent Technology episodes" and letting the LLM find the good ones. Technology publishes thousands of episodes a day, so the pool would be near-random and maybe 2% relevant. Filtering at the show level first gets that to roughly 30%. That single decision does more for pick quality than any amount of prompt tuning.

### The tradeoff, and it fails silently

**Those 200 shows are a ceiling.** The user can only ever be recommended something on that list. If the search step goes wrong, discovery is permanently capped and nothing in the product will tell you.

Guards:

1. Keep the list large (200, not 50). Polling is the only ongoing cost and it is cheap.
2. Rebuild monthly so new shows can enter.
3. Read the list once by hand at build time. See the Block 2 check in the implementation plan.

---

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | Best RSS and scheduling libraries. |
| Storage | SQLite | One file, zero setup. Swap to Postgres later by changing a connection string. |
| Web | Flask | Onboarding page is two routes. Do not reach for anything bigger. |
| Catalogue | Podcast Index API | Free, open, ~4M feeds. |
| LLM | `groq` SDK | Search-term expansion and the ranker. |
| Email | Resend | Simple API, good deliverability, free tier covers this. |
| Schedule | macOS `launchd` | **Not cron.** See section 7. |

Libraries: `httpx`, `feedparser`, `groq`, `jinja2`, `resend`, `flask`, `python-dotenv`.

---

## 4. Files

```
podcaster/
  .env              keys, never committed
  config.py         all tunable numbers in one place
  db.py             schema + connection

  podcastindex.py   API client
  universe.py       builds the 200-show list

  app.py            Flask: onboarding page + form handler
  templates/
    onboard.html
    done.html
    digest.html     the email

  fetch.py          pull new episodes from the 200
  rank.py           Groq model picks the best 2  <- the product
  email.py          render + send
  run.py            the every-other-day job

  podcaster.db
```

Each pipeline stage is a separate file on purpose. When a pick is bad you want to answer "which stage caused this" in ten seconds. That matters more than usual here, because the failure mode is subtle (mediocre picks) rather than loud (a crash).

---

## 5. Database

```sql
user
  id, email, created_at, last_run_at, status

interest
  id, user_id, text            -- "AI agents in production"

candidate_show                 -- the 200
  id, user_id, feed_id, feed_url, show_name, status

episode                        -- cache, shared across users
  id, guid UNIQUE, feed_id, show_name, title, description,
  duration_sec, published_at, web_url

digest
  id, user_id, ran_at, kind    -- 'sent' | 'quiet'

digest_item
  id, digest_id, episode_id, score, reason_text
```

**Two invariants that will save you:**

1. **Dedupe on `guid`, never on title.** Feeds republish episodes with edited titles constantly. Title-based dedupe means sending the same episode twice, which is the single thing most likely to make this feel broken.
2. **Never re-send an episode** already in a `digest_item` for that user. Enforce this in `fetch.py`, before the ranker ever sees it. The LLM must never be responsible for remembering.

Every table is already `user_id` scoped, so multi-user needs no schema change.

---

## 6. How a run works

### Stage 1: fetch

For each of the 200 `candidate_show` rows, get episodes published since `user.last_run_at`. Run 15 concurrently or this takes minutes instead of seconds.

**Use `last_run_at`, not "the last 2 days."** A fixed window silently drops episodes whenever a run fails or the laptop was asleep. Using the last successful run means a missed Wednesday gets picked up on Friday. Cap the lookback at 5 days so a long gap does not produce a flood.

Upsert into `episode` on `guid`.

### Stage 2: filter

Cheap, deterministic, no LLM:

- drop anything already sent to this user
- drop descriptions under 100 characters
- drop trailers and anything under 3 minutes
- dedupe on `guid`

The description rule does the most work. The ranker cannot judge what it cannot read, and thin descriptions are the biggest single source of bad picks.

### Stage 3: rank

One Groq call. **This is the product, everything else is plumbing.**

```
SYSTEM
You rank podcast episodes for one specific listener. You are their
filter, not their feed. Your job is to protect their time.

Rules, in priority order:
1. Topical fit beats popularity. A small show that nails their exact
   interest beats a big show that is merely adjacent.
2. Prefer episodes whose description names a concrete claim, guest, or
   case study. Vague descriptions score low even when on-topic.
3. Your reason MUST reference something specific from that episode's
   description. Generic praise ("a great listen", "highly relevant")
   is a failed output.
4. Score 0-100. 70 means "worth their time". Be harsh. Most episodes
   are not worth most people's time.

Returning fewer picks than asked for is CORRECT when the candidates
do not deserve it. An empty array is a valid and often correct answer.
Never pad.

Output ONLY valid JSON:
{"picks":[{"id":<int>,"score":<int>,"reason":"<one sentence>"}]}

USER
Interests: {free text, verbatim}

Candidates:
[1] show: ... | title: ... | 42m | desc: {first 400 chars}
[2] ...

Return up to 2 picks scoring 70 or above.
```

Implementation notes:

- Truncate descriptions to 400 characters. Past that is sponsor reads and link dumps that dilute the signal.
- On malformed JSON, retry once with the error appended, then return empty rather than crash. A quiet day beats a stack trace.
- Log every prompt and response to a file. When picks are bad this is how you find out why, and you will read it constantly in week one.

### Stage 4: deliver

Drop anything below `RELEVANCE_BAR`. If nothing survives, record a `quiet` digest and send nothing. Otherwise render `digest.html` and send via Resend.

**Write the digest rows before sending, mark sent after,** so a failed send is not silently lost.

---

## 7. Scheduling

**Use launchd, not cron.** A MacBook sleeps. Cron jobs scheduled during sleep are skipped entirely and never made up. launchd fires the missed job on wake. On a laptop this is the difference between a job that works and one that quietly stops.

`~/Library/LaunchAgents/com.ayush.podcaster.plist`, Sun/Mon/Wed/Fri at 07:00.

The `since = last_run_at` window in Stage 1 is what makes a missed run harmless rather than lossy.

---

## 8. Onboarding page

Two routes in `app.py`.

**`GET /`** renders the form:

- ~20 interest chips (Technology and AI, Business and Startups, Science, History, Finance, Culture, Politics, Health, Comedy, Crime, Sport, Personal Development). Pick 3 or more.
- A free text box: "What specifically, within those?"
- Email field

The chips exist only to beat the blank page. **The free text is where the actual signal lives.** "AI agents in production" is worth ten times the Technology chip, because it goes into the ranking prompt word for word and into the search expansion. Every layer of abstraction between the user's words and the prompt costs relevance.

This is also why there is no personality or archetype quiz. A quiz produces a label that then has to be decoded back into topics. Asking directly skips the lossy step, and takes less of your build.

**`POST /subscribe`** creates the user, saves interests, runs `universe.build()`, renders the confirmation.

The universe build takes ~30 seconds. Either show a "setting up your first digest" state or fire it in a thread and return immediately.

---

## 9. Config

Every tunable in `config.py`, nothing buried in logic:

```python
RELEVANCE_BAR         = 70    # the most important number here
PICKS_PER_EMAIL       = 2
UNIVERSE_TARGET       = 200
TERMS_PER_INTEREST    = 18    # measured minimum for a 3-interest fresh universe
MIN_DESC_CHARS        = 100
DESC_TRUNCATE         = 400
MAX_LOOKBACK_DAYS     = 5
FEED_WORKERS          = 15
```

`RELEVANCE_BAR` deserves attention. It alone decides whether this feels curated or spammy, and 70 is a guess. Expect to move it in week one. Keeping it here, rather than baked into the prompt, is why calibrating takes seconds instead of a rewrite.

---

## 10. Observability

The failure mode is silent, so log one line per run:

```json
{"ran_at":"...","candidates":140,"after_filter":72,
 "cleared_bar":3,"sent":2,"kind":"sent"}
```

Three numbers, and each tells you something different:

| Signal | What it means |
|---|---|
| `after_filter` very low | Universe too narrow. Looks identical to "a quiet week" from outside, but it is a bug. |
| `cleared_bar` always 0 or 1 | Bar too high, or pool too shallow. Check `after_filter` to tell which. |
| `cleared_bar` always at the cap | Bar too low, filler is leaking in. This is the worse direction to be wrong in. |

---

## 11. Path to v2

Nothing here blocks any of it:

| Change | Effort |
|---|---|
| Notifications from followed shows | Add a `followed_show` table, a second section in the email. The universe and ranker do not change. |
| Thumbs up and down, muting shows | Signed-token GET routes, `status` column already exists on `candidate_show`. |
| Hosting for other users | SQLite to Postgres is a connection string. Schema is already user-scoped. |
| Per-user send times | Add `timezone`, group runs by offset. |
