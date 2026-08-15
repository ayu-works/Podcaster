# PRD: Podcaster

**Every other day, one short email: what dropped from the shows you follow, plus something new worth your time.**

| | |
|---|---|
| Owner | Ayush Mayank |
| Status | Draft v2, for review |
| Date | 15 August 2026 |
| Build budget | 3 hours (v1) |
| Changed in v2 | Cadence moved from weekly to alternate day. Problem 1 demoted from a problem to a design constraint. Subscribed-show alerts pulled into v1 scope. Onboarding specified as chips plus free text, archetype test parked to v2. |

---

## 1. Problem

Podcast apps are built for *playback*, not for *deciding what to play*. Two failures do the real damage.

### Problem A: the discovery ceiling

New shows only surface through platform charts (dominated by celebrity and true crime) or word of mouth. There is no mechanism anywhere that says "this specific episode, from a show you have never heard of, is about the exact thing you care about."

The result is a listener whose rotation calcifies. They subscribed to their favourite shows two years ago and have added almost nothing since, not because nothing good was made, but because nothing good was ever put in front of them. The market for their attention is enormous and completely illegible to them.

### Problem B: the notification gap

Push notifications are binary and both settings fail. Off means a great episode drops on Tuesday and is never seen. On means every show the user follows fires an alert, the alerts become wallpaper, and they get dismissed without reading. There is no middle setting for "tell me when it actually matters."

This is worse than it sounds because it is invisible. The user does not know what they missed. They never file a complaint, they just quietly listen to less than they meant to.

### The constraint (formerly "problem 1")

An earlier draft listed backlog overwhelm as a third problem. On reflection it is not a problem the user would ever hire a product to solve. Nobody wakes up wanting their unplayed count reduced.

But it matters as a **design constraint**, because it is the exact failure mode that kills the solution to Problem B. If we alert on every new episode from every followed show, we have rebuilt the firehose in email, which is strictly worse than the push notifications the user already turned off.

**So the constraint is: notifying about followed shows must be selective, or Problem B's solution becomes Problem B's cause.** This single line governs most of the design decisions below.

### Why now
Episode-level metadata is available through free APIs, and LLMs can now read that metadata and judge relevance against a stated interest in a way keyword matching never could. The filtering layer is finally buildable by one person in an afternoon.

---

## 2. The user

**The intentional listener.** Listens 3 to 6 hours a week in 3 or 4 sessions, usually commuting or walking. Follows 10 to 30 shows across a few topic areas. Treats podcasts as a learning input, not background noise. Cares which *episode*, not just which show. Lives in email already and will not install another app to fix this.

That listening pattern (3 to 4 sessions a week) is the direct argument for alternate-day cadence: roughly one email per listening session, arriving while the episode is still current.

---

## 3. Cadence decision: alternate day

Runs land **Monday, Wednesday, Friday, Sunday**. Sunday covers weekend listening, Monday covers the commute.

**Why alternate day beats weekly**

- Most podcasts publish Tuesday to Thursday. In a Sunday-only digest, the best episode of the week is five days stale before the user hears about it.
- Alternate day is close enough to real time to actually address Problem B. A weekly roundup is an archive, not a notification.
- It matches the listening rhythm. One email per session is the right shape.

**What alternate day costs, and how it is paid for**

Each run sees roughly 2 days of episodes instead of 7, so the candidate pool per run drops by about 70%. Sending the same 5 to 7 picks from a pool that small means scraping the barrel, and filler is the one thing that kills this product.

The fix is that **digest size shrinks with cadence.** Target 2 episodes per email, hard maximum 3.

**The weekly budget**

To stop 4 sends a week from quietly becoming volume creep:

- Each user has a budget of **8 episode slots per week**.
- Each run may spend up to 3.
- Unspent budget carries over, capped at 2, so a quiet stretch never produces a 6-item dump.
- Ceiling of 8 against a 4 to 6 episode listening week means slight oversupply, enough for choice, not enough to feel behind.

**Quality gate and quiet days**

The ranker returns a 0 to 100 relevance score per candidate. Only candidates above the bar (start at 70, calibrate during self-testing) are eligible. If a run finds nothing above the bar, it sends a **one-line quiet-day note** rather than filler.

Guardrails on the quiet-day note, because a note about nothing is still an email:

- One line, no images, no CTA. "Nothing worth your time since Wednesday. Next check Sunday."
- Maximum one per week. A second consecutive quiet run sends nothing at all.
- If quiet days exceed one a week in testing, the candidate pool is too narrow. That is a pipeline bug, not a content reality.

---

## 4. Target journey (v1)

| Step | What happens |
|---|---|
| Onboard, 90 seconds | Chips, then free text, then format. See section 4a. |
| Mon/Wed/Fri/Sun, 7am | Short email arrives. Two sections. |
| **Section 1: From shows you follow** | 0 to 2 new episodes from followed shows, only those clearing the relevance bar. Solves Problem B without recreating the firehose. |
| **Section 2: Worth discovering** | 1 to 2 episodes from shows the user has never been sent. Solves Problem A. At least one discovery slot is protected in every send. |
| Each item shows | Title, show, duration, a one-line **why this is for you** referencing something real from the description, listen link, thumbs up or down. |
| Scan | Read in under 45 seconds. This is the entire product surface. |
| Act | Tap 1, it opens in their podcast app. |
| Compounding | Thumbs up on a discovery promotes that show into the follows list. The follows section grows by itself. |

The promise: **two episodes, chosen for you, every other day, and silence when there is nothing good.**

---

## 4a. Onboarding: chips, not an archetype test

The onboarding decides the quality of every email that follows, because the topic profile is the only input the ranker has. Three screens, 90 seconds total.

**Screen 1: interest chips.** About 20 chips across broad domains (Technology and AI, Business and Startups, Science, History, Health and Fitness, Finance and Markets, Culture and Media, Politics and Policy, Sport, Comedy, Crime, Personal Development). Pick 3 or more. This exists purely to defeat the blank page.

**Screen 2: free text refinement.** "What specifically, within those?" prefilled with a placeholder drawn from their chips. This is where the actual signal lives. "AI agents in production" is worth ten times more to the ranker than the Technology chip, because it goes into the prompt at full resolution.

**Screen 3: format fit, 3 questions.** Typical episode length they finish (under 30 / 30 to 60 / 60 plus / no preference), preferred shape (interview, narrative, solo essay, panel, no preference), and tolerance for a show that argues against their view (yes, keep me honest / mostly agree with me). Three taps, and it captures signal the topic chips structurally cannot.

### Why not an archetype test

The instinct behind it is right: a blank text box is a bad first screen. But an archetype test is the wrong fix, for four reasons.

1. **It is lossy compression in the wrong direction.** A test produces a label ("The Systems Thinker") which then has to be decoded back into topics to be usable. Chips plus free text hand the ranker the topics directly, at full fidelity. Every layer of indirection between the user's words and the LLM prompt costs relevance.
2. **Personality predicts format, not subject.** Which is exactly what screen 3 captures in three taps instead of ten questions. Knowing someone is analytical tells you very little about whether they want AI infrastructure or Indian macro. Asking them tells you everything.
3. **It taxes conversion before any value is delivered.** Ten questions ahead of a single episode is a heavy ask for a product that has not yet proven it can pick well.
4. **It costs a third of the build.** Question design, scoring, archetype to topic mapping, and a results screen is 60 to 90 minutes of a 180 minute budget, spent on onboarding, in a product whose entire risk sits in the ranking prompt. Chips plus free text plus 3 format questions is about 15 minutes.

**Keep the archetype test on the roadmap, as a growth surface rather than a ranking input.** "Which podcast listener are you?" with a shareable result is genuinely good acquisition, and it is worth building once the ranker is proven and the constraint has shifted from quality to signups. It is a v2 marketing asset wearing a v1 product costume.

---

## 5. The cold start problem (needs your call)

You chose to infer the follows list from thumbs-up feedback rather than importing subscriptions. That keeps onboarding clean and I agree with the direction, but it creates a hole worth naming plainly:

**Section 1 has nothing in it until the user has thumbed up several episodes.** For the first 2 to 3 weeks the email is discovery-only, which means Problem B, one of the two problems we just decided to focus on, is not solved until roughly week 3. If the user churns in week 1, it is never solved at all.

**Proposed fix, one line of onboarding:** ask the user to type the names of 3 to 5 shows they already listen to. Not an OPML export, not RSS URLs, not OAuth. Just names in a text box, resolved to feeds through a Podcast Index search call. About 15 minutes of build.

This is still inference (the list stays fuzzy and grows from behaviour), it just does not start from zero. Section 1 works on day one instead of week three.

**Flagging it rather than assuming it.** If you would rather protect the 90 second onboarding and accept a discovery-only first fortnight, that is a legitimate call, it just means v1 tests Problem A properly and Problem B barely.

---

## 6. Scope

### v1 (this 3 hour build)

| # | Requirement | Detail |
|---|---|---|
| R1 | Onboarding | Email, interest chips, free-text refinement, 3 format questions (section 4a), plus 3 to 5 show names (pending your call on section 5). |
| R2 | Candidate fetch | Episodes published in the last 2 days, from followed feeds plus a topic-mapped category sweep. Drop anything with a description under 100 characters before ranking. |
| R3 | LLM ranking | A Groq-hosted model scores candidates 0 to 100 against the topic profile and returns a one-sentence justification per pick. |
| R4 | Budget and gate | Apply the 70 threshold, the 3-per-run cap, and the 8-per-week budget. Decide send, quiet note, or silence. |
| R5 | Two-section email | Follows on top, discoveries below. Mobile-first HTML, minimal links. |
| R6 | Scheduler | Cron on Mon/Wed/Fri/Sun. Idempotent, never sends the same episode twice to the same user. |
| R7 | Feedback | Thumbs up or down as signed-token GET links, no login. Thumbs up on a discovery promotes the show into follows. |

Note that R7 is load-bearing in v1 in a way it was not in v2's predecessor. With subscriptions inferred rather than imported, feedback is the only mechanism that populates Section 1. It cannot be deferred.

### v1.1

- Proper subscription import (search-and-add, or OPML) once the digest quality is proven.
- User-tunable cadence and per-run size.
- A must-not-miss flag on 2 or 3 shows that bypasses the relevance gate entirely.

### Explicit non-goals for v1

No audio playback, no accounts or passwords, no mobile app, no push or browser notifications, no Spotify or Apple OAuth, no transcripts or summaries, no social or sharing features. Each is a 3 hour build on its own.

---

## 7. Data model

```
user
  id, email, created_at, status, weekly_budget (default 8), budget_carry

interest
  id, user_id, text, source (chip | freetext), weight

format_pref
  user_id, length_pref, shape_pref, challenge_tolerance

followed_show
  id, user_id, show_id, source (seeded | promoted), created_at
                                 # promoted = came from a thumbs up

episode                          # cached catalogue, shared across users
  id, guid, show_id, show_name, title, description,
  duration_sec, published_at, audio_url, web_url

digest
  id, user_id, sent_at, kind (full | quiet | skipped), slots_spent

digest_item
  id, digest_id, episode_id, section (follows | discover), rank,
  score, reason_text

feedback
  id, digest_item_id, value, created_at
```

Two rules that keep this honest: dedupe on `episode.guid`, never on title, because feeds republish. And never re-serve an episode present in any prior `digest_item` for that user.

---

## 8. Ranking logic (the actual product)

The pipeline is commodity. The judgement in R3 is the entire differentiator.

1. **Topical fit over popularity.** A 400-listener show that nails the user's topic beats a chart-topper that is merely adjacent.
2. **Specificity.** Prefer episodes whose description names a concrete claim, guest, or case study.
3. **Honest reasons.** The why-this line must reference something real from the description. "A great listen" is a bug, not a weak output.
4. **A followed show gets no free pass.** Same 70 bar as everything else. This is the constraint from section 1 doing its work.
5. **Protected discovery slot.** Every send reserves at least one slot for a show never sent to this user, even on a strong week for followed shows.
6. **Variety.** Never 2 episodes from the same show in one send. Mix in a sub-30-minute option where possible.

**Anti-goal: engagement maximisation.** If the right answer for a given run is one episode, send one. If it is zero, send the quiet note. Credibility is the whole asset and it is spent in a single filler email.

---

## 9. Success metrics

| Metric | Definition | v1 target |
|---|---|---|
| **North star: useful picks per send** | Episodes clicked or thumbed up, over episodes sent | ≥ 0.40 (about 1 of 2) |
| Open rate | Per send | ≥ 55% |
| Discovery rate | Clicked episodes from shows not previously sent | ≥ 30% |
| Follows section precision | Thumbs up over sends, Section 1 only | ≥ 0.50 |
| Week 3 retention | Still opening at week 3 | ≥ 50% |
| Unsubscribe | Per send | < 1.5% |
| Quiet day frequency | Quiet or skipped runs per week | ≤ 1 |

The bar per email is deliberately higher than the weekly version's was, and the unsubscribe tolerance lower. At 4 sends a week each email is a smaller ask and must justify itself individually.

Counter-metric: high discovery rate with high thumbs-down means the ranker is being novel rather than relevant. Relevance wins.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Cold-start empty follows section | Problem B unsolved for 3 weeks | Section 5. Seed follows at onboarding. |
| 4x cadence, 4x unsubscribe exposure | Fast churn | Hard 8-slot weekly budget, 70 relevance gate, quiet days instead of filler. |
| Thin candidate pool per run | Repetitive or weak picks | Widen the category sweep, drop thin descriptions before ranking, monitor quiet-day frequency as the early warning. |
| Filler creeps in to fill the cadence | Product dies quietly | Treat a filler email as a P0 bug. Ayush is user zero and ships to nobody else until 3 consecutive runs produce picks he would actually play. |
| Spam or Promotions folder, worse at 4x/week | Product invisible | Proper sender (Resend or Postmark), SPF and DKIM, plain sender name, few links. |
| LLM cost at 4 runs a week | Scaling cost | Cheap keyword prefilter before the LLM pass, one batched call per run, shared episode cache across users. |
| Free API rate limits | Silent pipeline failure | Shared episode table, retry with backoff, alert on any zero-candidate run. |

---

## 11. Does v1 fit in 3 hours?

Roughly, and only because of what is cut. The additions since v2's predecessor (two-section email, budget logic, follow promotion) are each 10 to 20 minutes of logic, not new systems. The cadence change is a cron expression. The real cost is the onboarding seed in section 5, at about 15 minutes.

If it runs long, the cut order is: quiet-day note first (just skip silently), then the two-section split (blend into one ranked list with a "following" tag). Do not cut the relevance gate or the feedback loop. Those are the product.

---

## 12. Open questions

1. **The section 5 call.** Seed follows at onboarding, or accept a discovery-only first fortnight?
1a. **Archetype test.** Parked as a v2 growth surface, see section 4a. Confirm you are happy with that.
2. **Catalogue source.** Podcast Index (free, ~4M feeds, key and secret auth, has recent-episode endpoints) with iTunes Search as an artwork and link fallback. Confirm.
3. **Relevance bar of 70.** A guess. It gets calibrated in self-testing, and it is the single number that most determines whether this feels curated or spammy.
4. **7am in which timezone?** Assuming Asia/Kolkata for user zero. Needs a per-user field before anyone else uses it.
5. **Personal tool or product?** If this is only for you, skip email infrastructure and render to a local HTML file. That is about an hour back, and email can be added once the picks are good.

---

## 13. What v1 deliberately does not prove

This build tests one hypothesis: **an LLM reading episode metadata against stated interests can pick, every other day, one or two episodes a real listener finds worth their time, and can stay quiet when it cannot.**

It does not test willingness to pay, subscription import at scale, or cost economics. If the hypothesis fails, none of the deferred work would have saved it.
