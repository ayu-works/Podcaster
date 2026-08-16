# Podcaster test cases

Run automated coverage with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite currently contains 85 passing tests. External calls are mocked;
separate live gates are listed at the end.

## S1–S2: config, schema, and clock

- All 20 topic slugs are unique and are the sole accepted slug set.
- Every topic has one or more official Podcast Index category IDs.
- The nine-table schema initializes idempotently.
- `episode.guid` is unique.
- Failed/running runs do not advance `last_good_cutoff`; `ok`/`partial` do.
- Local database connections accept both `Path` and `file:` inputs.

## S3: dynamic discovery

- Per-topic recent feed results merge by feed ID and retain all matched topics.
- Round-robin selection prevents a large category from monopolizing the cap.
- Cached discovery preserves muted shows and does not delete historical cache.
- Episode lookup joins at most 200 comma-separated feed IDs.
- 450 selected feeds require exactly three episode requests.
- A refreshed fetch uses dynamic discovery rather than the reference CSV.

## S4: fetch and filter

- The cutoff is committed before the first external request.
- A publication during a run remains inside the next run's window.
- GUID upsert updates without duplication; title is never the identity.
- Trailers/bonuses, thin cleaned descriptions, short episodes, and duplicate
  GUIDs drop at exact boundaries.
- Partial request failures retain good batches; every batch failing raises.
- One subscriber's sent history does not hide shared episodes.

## S5: tag once

- Valid JSON writes score, why, known topics, tag time, and attempt state.
- Unknown slugs drop while valid slugs remain; topic count is capped.
- Empty topics are valid and intentionally produce no topic match.
- Generic/ungrounded reasons do not become tagged output.
- Malformed responses retry within the bounded per-episode attempt count.
- Completed batches survive a later API failure.
- Daily budget exhaustion leaves unattempted rows untouched and visible.

Manual quality gate: read accepted why-lines; each must name a claim, guest,
case study, or number actually present in the supplied description.

## S6: curate

- Scores below `RELEVANCE_BAR` do not appear and quiet topics are not padded.
- Rank order, per-topic cap, and per-show cap are deterministic.
- A cross-topic episode may appear in both shared topic lists.
- Late-tagged but fresh episodes are eligible; stale publications are not.
- Rerunning the same run is idempotent; later runs do not give a second
  editorial shot.

## S7: email and multiple subscribers

- Subscribers receive only subscribed topic picks, grouped by topic.
- Cross-topic matches dedupe; the whole email respects item and show caps.
- Pending/sent history excludes; failed history retries and increments attempts.
- Two subscribers can receive the same shared pick independently.
- Empty, pending, paused, and unsubscribed recipients are not mailed.
- A committed pending row is observable from a second connection before the
  mocked Resend call begins.
- Failure for recipient two does not block recipients one or three.
- Escaping, duration, listen URL, unsubscribe URL, 600px width, no images, and
  the 102KB ceiling are checked.
- One-click unsubscribe headers are present.
- The canonical episode page is preferred as `web_url`; an audio enclosure is
  never stored, including when a feed copies it into `link`.
- An episode with no page falls back to the show's Apple page, then to no link.
- A rendered title and CTA never point at an MP3/M4A or other media file, even
  when the stored row predates the rule; the title still renders unlinked.
- Repairing legacy rows clears only media URLs and is idempotent.

Manual gate: open a real digest on a phone and decide whether at least one
episode is genuinely worth playing.

## S8: onboarding and consent

- GET `/` renders exactly 20 topic checkboxes, email, and no free text/polling.
- Valid signup stores exactly selected known topics and remains pending.
- Invalid email, zero topics, and unknown slugs write no partial state.
- Confirmation activates idempotently and stamps `confirmed_at`.
- Duplicate active signup updates the topic set without a second subscriber.
- Confirmation and unsubscribe tokens are distinct, long, and rotated when an
  unsubscribed address returns.
- GET unsubscribe is read-only; valid, repeated, and unknown POSTs all succeed
  without revealing token validity.
- Honeypot creates no row or email; the sixth per-IP hourly attempt is limited.

## S9: orchestration

- Stage order is discover/fetch -> tag -> curate -> send.
- Fetch/tag/curate failure halts before email, marks `failed`, and leaves the
  prior good cutoff authoritative.
- Recipient failure produces `partial` and the new cutoff advances.
- Skip flags make no calls to skipped external stages.
- Every attempted run appends all required JSON metrics, including queue,
  percentile, pick, subscriber, and delivery signals.
- Workflow cron is `30 1 * * 0,1,3,5`, manual dispatch exists, credentials come
  from secrets/variables, and logs upload with `if: always()`.
- Main workflow stages are visible separately; Fetch is limited to 180 seconds,
  Tag to 100 episodes/240 seconds, and the whole job to ten minutes. The real
  short digest is limited to 30 feeds, 10 taggable episodes, two emailed picks,
  and 120 seconds including setup.
- There is no monthly static seed: every run refreshes recent category feeds.

## External acceptance gates

These cannot pass until hosted credentials and domain setup exist:

1. Connect to real Turso and prove named row access for every query.
2. Deploy Vercel; signup and confirmation work with all local machines off.
3. Verify a Resend domain and deliver to a second, non-owner address.
4. Trigger GitHub `workflow_dispatch` with the laptop off; observe a complete
   Turso-backed run and downloadable logs.
5. Trigger twice and confirm the second creates no duplicate sends.
6. Prefetch the deployed unsubscribe GET with `curl`; status remains active.
