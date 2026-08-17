# Podcaster product requirements

## Problem

Podcast discovery produces too much inventory and too little judgement.
Podcaster should deliver a small, trustworthy list of newly released episodes
that match subjects a listener chose, without requiring them to follow shows or
maintain a personal feed list.

## Product promise

- Up to ten strong episodes on Sun/Mon/Wed/Fri mornings.
- Nothing is sent when nothing clears the quality threshold.
- Each recommendation says concretely why the episode may be worth the time.
- Subscribers choose broad topics and can unsubscribe safely.
- The service works in the cloud with no owner laptop involved.

## Decisions

### Discover channels dynamically

Do not maintain or poll “all podcasts.” Each run asks Podcast Index which feeds
were recently active in official categories mapped to the 20 product topics.
It then balances that bounded channel set and requests recent episodes in
feed-ID batches. This gives current category coverage without storing a giant
permanent universe.

The catalogue category is only a candidate source. Episode meaning and quality
come from one-time tagging of the episode title/description.

### Tag once, personalize in SQL

The model returns 0–3 topic slugs, a 0–100 score, and one description-grounded
why-line per episode. These values are stored globally. Subscriber delivery is
then a SQL join over their selected slugs, so marginal model cost per subscriber
is zero.

### Topics, not free text

Arbitrary prose has nothing to join against without embeddings or another
per-user model pass. If the taxonomy feels coarse, split topic slugs rather
than adding an input the architecture cannot honor.

### Silence is output

No quiet-day note and no padding. A low-volume email is acceptable; an empty
one is not sent. Repeatedly quiet topics are an observable coverage or threshold
problem.

## Functional requirements

| ID | Requirement |
|---|---|
| R1 | Query recent feeds for all configured category mappings every run; dedupe and balance to `DISCOVERY_FEED_TARGET`. |
| R2 | Fetch episodes with at most 200 feed IDs per Podcast Index request, since the last good pre-fetch cutoff. |
| R3 | Drop unusable episode types, missing/duplicate GUIDs, thin descriptions, and short promos before model work. |
| R4 | Tag each new episode once with known slugs, strict score, and grounded reason; bound retries and daily spend. |
| R5 | Curate at most ten shared picks per topic with relevance, staleness, and per-show guarantees. |
| R6 | Merge picks per active subscriber, dedupe, cap at ten, respect per-user history, and make zero model calls. |
| R7 | Commit pending delivery attempts before Resend; retry failed, never blindly retry ambiguous pending. |
| R8 | ~~Require double opt-in~~ superseded 2026-08-17 by single opt-in plus a welcome mail carrying a prominent one-click removal (`doc/single-opt-in.md`); tokenized GET unsubscribe is read-only and POST is idempotent. |
| R9 | Run in GitHub Actions at 07:00 IST Sun/Mon/Wed/Fri, with Turso state and logs uploaded on every outcome. |
| R10 | Log discovery, fetch, tag queue/cost/distribution, curation, subscriber, delivery, and status metrics per run. |

## Non-functional requirements

- Cost scales with new episodes, not subscriber count.
- No subscriber can affect another's fetched or curated pool.
- A missed/late scheduled job loses no publication window within the lookback cap.
- A stage failure never sends a half-built digest.
- One recipient failure never blocks the next recipient.
- Feed-provided text is escaped in email and external images are unnecessary.
- Secrets live only in local environment files or hosted secret stores.

## Capacity and quality

The Groq free daily token limit is the binding constraint. Live measurement
showed 280 balanced recent feeds yielding 872 usable rolling-24-hour episodes.
The production target of 240 projects roughly 747, leaving retry headroom.
This must be re-measured when categories, schedule, model, prompt, or provider
limits change.

`RELEVANCE_BAR=70` is the primary product-quality lever. It is enforced after
the model, not merely requested in a prompt. Manual acceptance is simple: the
owner should genuinely want to play at least one episode from a real digest.

## Risks and signals

| Risk | Signal / response |
|---|---|
| Category under-coverage | Repeated zero picks plus low discovery cache coverage; adjust mappings or target. |
| Catalogue/API outage | All request batches failing raises; never interpret it as a quiet day. |
| Tag quality collapse | Score percentile/topic distribution drift and manual why-line review. |
| Daily budget too small | `untagged_left` grows each run; lower volume/input or raise tier. |
| Duplicate/ambiguous email | Pending-before-send state prevents blind retry. |
| Sending-domain failure | `emails_failed` trend; verify domain, SPF/DKIM, and Resend status. |
| Scheduler silently stops | Detect absence of new run records, not only explicit failed records. |
| Unwanted enrollment | Pending users are inert until confirmation. |
| Scanner unsubscribe | GET never mutates; only POST does. |

## Release blockers

Local implementation and automated tests are complete. Public release requires
a verified Resend domain, Turso credentials, Vercel deployment with
`PUBLIC_BASE_URL`, hosted secrets, a real second-address delivery, and a manual
GitHub Action run completed while the owner's computer is off.
