# PRD: Podcaster

**Every other day, one short email: the best new episodes in the topics you care about.**

| | |
|---|---|
| Owner | Ayush Mayank |
| Status | Draft v3, for review |
| Date | 16 August 2026 |
| Build budget | ~4 hours |
| Changed in v3 | Rebuilt around a **shared episode pool**: episodes are tagged once at ingest, not ranked per user at send. Multi-subscriber is now the point rather than a v2 note. Problem B (followed shows) cut from scope. Free-text interests removed; topics are a fixed checklist. Weekly budget, carryover, quiet-day note, format preferences and feedback links all cut. |

---

## 1. Problem

Podcast apps are built for *playback*, not for *deciding what to play*.

### Problem A: the discovery ceiling

New shows only surface through platform charts (dominated by celebrity and true crime) or word of mouth. There is no mechanism anywhere that says "this specific episode, from a show you have never heard of, is about the exact thing you care about."

The result is a listener whose rotation calcifies. They subscribed to their favourite shows two years ago and have added almost nothing since, not because nothing good was made, but because nothing good was ever put in front of them. The market for their attention is enormous and completely illegible to them.

**This is the only problem v3 addresses, and it is addressed properly.**

### Problem B: the notification gap — cut from scope

v2 also targeted the gap between push-notifications-off (a great episode drops on Tuesday and is never seen) and push-notifications-on (every followed show fires an alert, alerts become wallpaper).

**This is cut, explicitly, not quietly deferred.** Solving it requires knowing which shows a user follows, which requires either subscription import or a feedback loop that promotes shows on a thumbs-up. Both were in v2 scope; both are now out. A product cannot notify you about shows you follow when it has no idea what you follow.

Cutting it also removes the dependency that made v2's cold-start problem unsolvable in week one (see section 5).

### The constraint

Backlog overwhelm is not a problem a user would hire a product to solve — nobody wakes up wanting their unplayed count reduced. But it remains a **design constraint**: if we send everything that matches a topic, we have rebuilt the firehose in email, which is strictly worse than the app the user already closed.

**So the constraint is: topic filtering must be selective, or the solution to Problem A becomes a new instance of the problem the pitch opens with.** This is what `RELEVANCE_BAR` and `PICKS_PER_TOPIC` exist to enforce, and it is the one number most likely to need moving in week one.

### Why now

Episode-level metadata is available through free APIs, and LLMs can now read that metadata and judge relevance in a way keyword matching never could. The filtering layer is finally buildable by one person in an afternoon.

---

## 2. The user

**The intentional listener.** Listens 3 to 6 hours a week in 3 or 4 sessions, usually commuting or walking. Follows 10 to 30 shows across a few topic areas. Treats podcasts as a learning input, not background noise. Cares which *episode*, not just which show. Lives in email already and will not install another app to fix this.

That listening pattern (3 to 4 sessions a week) is the direct argument for alternate-day cadence: roughly one email per listening session, arriving while the episode is still current.

**New in v3: there are many of them.** v2 was a personal tool with a `user_id` column. v3 is built for a subscriber list, and that assumption drives every architectural decision in section 7.

---

## 3. Cadence decision: alternate day

Runs land **Monday, Wednesday, Friday, Sunday** at 07:00. Sunday covers weekend listening, Monday covers the commute.

**Why alternate day beats weekly**

- Most podcasts publish Tuesday to Thursday. In a Sunday-only digest, the best episode of the week is five days stale before the user hears about it.
- It matches the listening rhythm. One email per session is the right shape.

**A note on the tagging cadence.** An early sketch of v3 had episodes ingested every other day but *tagged weekly*. That does not work: an episode fetched on Tuesday would have no topic until Sunday and so could not be emailed on Wednesday. Tagging runs at ingest, in the same job as the fetch. There is no separate weekly pass.

**Quality gate and quiet topics**

The tagger returns a 0 to 100 score per episode. Only episodes at or above the bar (start at 70, calibrate during self-testing) are eligible for any topic list. If a topic has nothing above the bar, subscribers to that topic **receive nothing** — no email, and no note about the absence.

v2 specified a one-line quiet-day note, capped at one a week. **Cut.** A note about nothing is still an email, the bookkeeping to cap it per user is real, and at 20 topics the case barely arises — a subscriber with three topics is very unlikely to have all three quiet at once. If quiet sends turn out to be common in testing, that is a pipeline bug (the universe under-covers those topics), not a content reality, and the fix belongs in section 10's risk table rather than in an apology email.

---

## 4. Target journey

| Step | What happens |
|---|---|
| Onboard, 30 seconds | Tick topics, enter email, done. See section 4a. |
| Mon/Wed/Fri/Sun, 7am | Short email arrives, grouped by topic. |
| Content | Up to 10 episodes total across the topics they ticked, each above the relevance bar, **at most 2 from any one show in the whole email** — enforced by `MAX_PER_SHOW_PER_EMAIL` at send time, not merely per topic list. |
| Each item shows | Title, show, duration, a one-line **why this** referencing something real from the description, listen link. |
| Scan | Read in under 45 seconds. This is the entire product surface. |
| Act | Tap one, it opens in their podcast app. |
| Quiet | Nothing above the bar in any of their topics means no email at all. |

The promise: **the best new episodes in your topics, every other day, and silence when there is nothing good.**

Note what changed from v2's promise, "two episodes, *chosen for you*". Picks are chosen per *topic*, not per person: two subscribers to `technology-ai` receive the same episodes, diverging over time only because they joined on different days and have already been sent different things. This is a deliberate trade and it is what makes the product affordable at scale. Marketing copy must not claim otherwise.

---

## 4a. Onboarding: topics only

Onboarding decides the quality of every email that follows, because the topic set is the only input the pipeline has. **One screen, 30 seconds.**

Twenty topics as checkboxes. Pick at least one. Enter an email. Submit.

### Why the free-text box was removed

v2's onboarding had three screens: chips, then a free-text refinement, then three format-fit questions. All three are cut. The free-text cut needs explaining, because v2 argued hard for the opposite.

**v2 said:** *"'AI agents in production' is worth ten times more to the ranker than the Technology chip, because it goes into the prompt at full resolution."*

**That is retracted.** It was true, but only because of an architecture that no longer exists. In v2 the user's literal words were pasted into a ranking prompt at send time, so full-resolution text genuinely was the signal. In v3 there is no prompt at send time — matching is `WHERE topic IN (...)` against a stored tag. Free text has nothing to join against. Supporting it would mean embedding every subscriber's words and every episode, then doing a per-user similarity pass at send time, which reintroduces exactly the per-user work the redesign removes.

**The evidence also went the other way.** v2's only real subscriber typed *"Technology & AI — anything in it"* and *"Business & Startups — just live it"* — chip labels with a shrug appended. The field was not used as designed even by the person who designed it.

**If a topic feels too coarse, the answer is more topics, not a text box.** Splitting `technology-ai` into `ai-engineering` / `ai-research` / `ai-business` costs a little prompt length and a re-seed. It recovers most of the specificity free text promised, at none of the architectural cost.

Screen 3 (format fit: length, shape, tolerance for disagreement) is also cut. It was per-user ranking input, and there is no per-user ranking. One trace survives in the tagging prompt: where two candidates are otherwise equal, prefer the shorter one.

The archetype test stays where v2 parked it — **a v2 growth surface wearing a v1 product costume**. "Which podcast listener are you?" with a shareable result is genuinely good acquisition, and worth building once tag quality is proven and the constraint has shifted from quality to signups.

---

## 5. Resolved: the cold start problem

v2 flagged this for the owner's call: follows were inferred from thumbs-up feedback, so *"Section 1 has nothing in it until the user has thumbed up several episodes"* — meaning Problem B went unsolved until roughly week 3, and never at all for anyone who churned in week 1.

**Resolved by deletion.** There are no followed shows in v3, so there is no empty section and no cold start. Every subscriber's first email is as good as their tenth, because the picks come from a shared pool that was already tagged before they signed up.

This is the clearest benefit of the shared-pool design, stated precisely: **a new subscriber's first email is drawn from a fully tagged pool, so it is exactly as good as everyone else's that day.** They do not receive a backfill of past picks — they join the current run like everyone else. What they inherit is a working pipeline, not a history.

---

## 6. Scope

### v3 (this build)

| # | Requirement | Detail |
|---|---|---|
| R1 | Global universe | Search across all 20 topics, `SHOW_TARGET` shows, not per user. Rebuilt monthly **by a scheduled workflow** (`.github/workflows/seed.yml`), not by hand. `SHOW_TARGET` is derived from the tagging token budget — see `ARCHITECTURE.md` section 9 — and is ~2,500 on Groq's free tier. |
| R2 | Onboarding | Email plus at least one topic checkbox. Synchronous, no background build. |
| R3 | Fetch | Every active show polled since the last **successful** run, capped at a 5-day lookback. Drop descriptions under 100 characters and anything under 3 minutes. |
| R4 | Tagging | A Groq-hosted model reads each new episode once and returns 0–3 topic slugs, a 0–100 score, and a one-sentence grounded reason. Batched, resumable, rate-limit aware. |
| R5 | Curation | Per topic: top `PICKS_PER_TOPIC` above the bar, at most 2 per show. Pure SQL, no AI. |
| R6 | Delivery | Per subscriber: their topics, deduped against `sent`, capped per show and per email. No qualifying picks means no email. Delivery is recorded as an **attempt** with `pending`/`sent`/`failed` state, so a bounced send retries rather than being recorded as delivered. |
| R7 | Unsubscribe | Tokenised, footer link, no login. `GET` renders a confirm page, `POST` acts — email link scanners prefetch URLs, so a mutating `GET` silently removes people. Plus `List-Unsubscribe` headers. |
| R10 | Double opt-in | New signups are `pending` until they click a confirmation link. Only `active` subscribers receive a digest. A public form that mails anyone on request is both an abuse vector and a deliverability risk. |
| R11 | Signup hosting | Vercel Python serverless, free tier, Turso over HTTP. Always-on: a signup link must work when nothing of the owner's is running. |
| R8 | Scheduler | **GitHub Actions** on Mon/Wed/Fri/Sun, plus a monthly seed workflow. Runs in the cloud, with no machine of the owner's involved. Idempotent. A failed pipeline stage does not advance the clock; an isolated delivery failure does. |
| R9 | Hosted database | **Turso (libSQL)**, since the Actions runner is ephemeral and the signup app must write to the same database the pipeline reads. |

R7 is load-bearing in a way it was not in v2. With more than one subscriber it is a legal requirement, not a courtesy.

### Cut from v2 scope, deliberately

Followed shows and the two-section email; thumbs up/down feedback and signed tokens; the eight-slot weekly budget and carryover; the quiet-day note; format preferences; the free-text interest field. Each was designed around per-user state that v3 does not maintain.

### v3.1

- Finer topic taxonomy (35 to 40 slugs) once coverage per topic is measured.
- Per-subscriber timezone, so 07:00 means 07:00 where they are.
- Optional per-user re-sort via embeddings, once tag quality is proven.

### Explicit non-goals

No audio playback, no accounts or passwords, no mobile app, no push or browser notifications, no Spotify or Apple OAuth, no transcripts or summaries, no social or sharing features, no free-text interests, no per-user personalisation of any kind.

---

## 7. Data model

Nine tables. See `ARCHITECTURE.md` section 5 for the full DDL and the reasoning.

```
subscriber       id, email UNIQUE, unsub_token UNIQUE, created_at, status
subscription     subscriber_id, topic
show             id, feed_id UNIQUE, feed_url, title, added_at, status
show_topic       show_id, topic            # coverage/debug only
episode          id, guid UNIQUE, feed_id, show_name, title, description,
                 duration_sec, published_at, web_url,
                 score, why, tagged_at     # AI output, written once
episode_topic    episode_id, topic         # the stored match
run              id, started_at, finished_at, fetched, tagged,
                 emails_sent, status
daily_pick       run_id, topic, episode_id, rank
sent             subscriber_id, episode_id, run_id, sent_at
```

**The one structural difference from v2, and the reason for this document:** `show` has no `user_id`, and `episode` carries its own `score`, `why` and topics. v2 computed the interest-to-episode match inside a prompt at send time and never stored it, so it had to be recomputed for every user on every run and could only ever see the ~25 candidates that fit in one rate-limited call. v3 writes the match down once. The pool is shared, the tagging cost is proportional to episodes rather than users, and every fetched episode is reachable instead of 15% of them.

Rules that keep this honest: dedupe on `episode.guid`, never on title, because feeds republish. Never re-serve an episode present in `sent` for that subscriber. Never let one subscriber's history narrow a fetch that is shared by everyone.

---

## 8. Tagging logic (the actual product)

The pipeline is commodity. The judgement in R4 is the entire differentiator.

Enforced **in the prompt**:

1. **Topical fit over popularity.** A 400-listener show that nails a topic beats a chart-topper that is merely adjacent.
2. **Specificity.** Prefer episodes whose description names a concrete claim, guest, or case study. Vague descriptions score low even when on-topic.
3. **Honest reasons.** The why-this line must reference something real from the description. "A great listen" is a bug, not a weak output. This is additionally enforced in code by `looks_generic()`, which diverts unfounded reasons to the retry queue rather than trusting the instruction.
4. **Empty is valid.** An episode that fits no topic returns an empty array and is dropped. This is expected and common.

Enforced **in code** (`curate.py`), because a prompt instruction is a request and a post-filter is a guarantee:

5. **The relevance bar.** Nothing below `RELEVANCE_BAR` reaches any list.
6. **Variety.** Never more than 2 episodes from the same show in one topic list.

**Anti-goal: engagement maximisation.** If the right answer for a topic is two episodes, send two. If it is zero, send nothing. Credibility is the whole asset and it is spent in a single filler email.

**What changed from v2's rules.** "A followed show gets no free pass" and "protected discovery slot" are gone — both presupposed followed shows. Their spirit survives structurally: with no follows, *every* pick is a discovery, so the discovery slot no longer needs protecting.

---

## 9. Success metrics

| Metric | Definition | Target |
|---|---|---|
| **Leading: tag quality** | Share of tagged episodes scoring ≥ 70, and the p50/p90 score spread | 10–25% above bar, p90 − p50 ≥ 15 |
| Open rate | Per send | ≥ 55% |
| Click rate | Sends with at least one click | ≥ 25% |
| Week 3 retention | Still opening at week 3 | ≥ 50% |
| Unsubscribe | Per send | < 1.5% |
| Topic coverage | Topics producing zero picks in a run | ≤ 3 of 20 |

**Tag quality is the leading indicator and replaces v2's north star.** v2's *"useful picks per send"* was defined as clicks-or-thumbs over episodes sent — but thumbs require feedback links, which are cut, and are therefore unmeasurable. Rather than keep a north star nobody can compute, v3 measures the stage everything else depends on.

The score spread matters as much as the level. If p50 and p90 sit within a few points, the model is not discriminating, curation has degenerated into recency ordering, and the bar is doing nothing — while every dashboard still looks healthy.

Counter-metric: high open rate with rising unsubscribes means the subject line is outperforming the contents.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Tagger degrades silently** | Every subscriber's email gets worse simultaneously, and nothing downstream notices | The single biggest risk in this design. Hand-read 20 tagged rows at build time; log score distribution every run; `looks_generic()` as an automated floor. |
| Universe under-covers a topic | Subscribers to that topic get nothing, indistinguishable from a quiet week | `show_topic` counts per topic checked at seed time; alert when a topic produces zero picks repeatedly. |
| Sandbox sender | Cannot deliver to anyone but the owner | Verify a domain, add SPF and DKIM, before a second subscriber exists. Blocking. |
| **Daily token cap bounds the product** | The universe cannot exceed ~2,500 shows on the free tier, capping topic coverage and therefore how specific topics can be | Groq free tier is 200K tokens/day, and a run costs ~170K. `SHOW_TARGET` is derived from this, not chosen. A paid tier is the fix and is a config change. Raising `SHOW_TARGET` without raising the tier silently truncates coverage — episodes get fetched and never tagged. |
| Rate limit stalls tagging | Untagged backlog compounds run over run | `tagged_at` makes tagging resumable; `tag_attempts` caps futile retries; `untagged_left` and `tokens_used` logged every run. |
| Late-tagged episodes stranded | An episode retried on a later run misses its curation window and is never sent | Curation selects on `tagged_at`, not `published_at`. Tested explicitly (S6-11). |
| Failed send recorded as delivered | Subscriber gets nothing and the retry excludes those episodes | `sent` carries `pending`/`sent`/`failed`; only `failed` is re-eligible. |
| Everyone gets the same email | Product feels generic, weakens the "for you" claim | Accepted trade. Mitigated by `sent` divergence and, if needed later, an embedding re-sort. Do not claim personalisation in copy. |
| Filler creeps in to fill 10 slots | Product dies quietly | Treat a filler email as a P0 bug. The bar is enforced in SQL, not in the prompt, precisely so it cannot be talked around. |
| **Scheduled workflow auto-disabled** | The product stops entirely and nothing reports it | GitHub disables scheduled workflows after ~60 days of repository inactivity. No run means no error, no log line, no alert — the most likely way this dies quietly. Monitor for a **gap** in `runs.jsonl`, not for a failure inside it. |
| Free-tier terms change | Scheduler or database disappears | Everything runs on free tiers by constraint. Turso is the newest and carries the most risk; Neon and Supabase are the fallbacks, at the cost of a Postgres dialect port. |
| Spam or Promotions folder | Product invisible | Proper sender, SPF and DKIM, plain sender name, few links, working unsubscribe. |
| No feedback loop at all | Cannot measure whether picks are good beyond opens and clicks | Accepted for v3. This is the strongest argument for adding feedback links back in v3.1. |

---

## 11. Does this fit the budget?

Roughly four hours, and the shape has changed. v2's cost was spread thinly across per-user machinery; v3 concentrates it in two places: the tagging stage (Step 5) and the documentation reconciliation (Step 10). Everything else is de-scoping — the onboarding block gets *smaller*, `fetch.py` loses a parameter, and the ranker's pool-truncation logic is deleted outright.

One new cost that did not exist before: **tagging takes 20 to 30 minutes of wall clock per run** on the free Groq tier. That is a batch job, not an interactive one. Build the `--limit` flag first so iteration does not require sitting through a full pass.

If it runs long, cut in this order: the monthly seed workflow (re-seed by hand for a month), topic granularity beyond 20, `--dry-run` flags, concurrency tuning. **The digest workflow itself is not cuttable** — with subscribers waiting on it, a pipeline nobody triggers sends nobody anything.

**Never cut:** the seed hand-read, the tag hand-read, the relevance bar, or the record-then-send ordering.

---

## 12. Open questions

1. **The relevance bar of 70.** Still a guess, still the single number that most determines whether this feels curated or spammy. Now more consequential than in v2: it gates every subscriber at once rather than one.
2. **`PICKS_PER_TOPIC = 10`.** Ten items is closer to a feed than a filter, and the pitch opens by complaining about volume. It is one constant. Ship at 10, read your own inbox for two weeks, decide.
3. **Is 20 topics too coarse now that free text is gone?** Genuinely unknown until real subscribers exist. The fix is cheap and additive.
4. **Timezone.** The schedule is UTC cron (01:30 = 07:00 IST). With one user this was theoretical; with a subscriber list it is wrong for everyone outside India. Needs a per-subscriber field and a send stage grouped by offset before any geographic spread.
5. **Pay for Groq, or accept ~2,500 shows?** This is the one open question with a cost attached. The free tier's 200K tokens/day caps the universe at roughly 2,500 shows, which is half what an earlier draft assumed. A paid developer tier removes the ceiling for a few dollars a month. Until that is decided, `SHOW_TARGET = 2500` and topic coverage is thinner than designed — check `show_topic` counts at seed time (S3-13) to see whether any topic is actually starved by it.

Resolved since v2: catalogue source (Podcast Index, confirmed); the archetype test (parked as growth); the section 5 cold-start call (deleted with follows); "personal tool or product?" (a subscriber list answers it).

---

## 13. What this build deliberately does not prove

This build tests one hypothesis: **an LLM reading episode metadata can tag and score new episodes well enough that a per-topic top-10, chosen with no knowledge of the individual reader, is still worth a real listener's time — and can stay quiet when it is not.**

It does not test willingness to pay, whether shared picks feel personal enough to retain, or cost economics at scale. If the hypothesis fails, none of the deferred work would have saved it.

The sharpest version of the risk: v2 bet that *personalisation* was the product. v3 bets that *filtering* is, and that the personalisation was mostly ceremony. If subscribers churn because the picks feel generic, that bet was wrong, and the embedding re-sort in section 6's v3.1 list is the first thing to try.
