# Block 2 — Dynamic catalogue discovery

Production discovery asks Podcast Index for recently updated English/unknown
feeds in the official categories mapped to all 20 product topics. Results are
deduplicated, topic-balanced, capped at `DISCOVERY_FEED_TARGET`, and cached in
`show`/`show_topic` for diagnostics and mute persistence.

`podcastindex.py` exposes `/recent/feeds` and batched `/episodes/byfeedid`.
`discover.py` performs the per-run merge and round-robin balance. One episode
request carries no more than 200 feed IDs.

The older `universe.py` search-term seed remains only as a reference/fallback
tool. It is not called by `block-7-run/run.py` and has no monthly workflow.
Its measured catalogue findings remain useful: roughly 70% of broad search
results were stale, search-term overlap was near zero, and one tested profile
went from 1,641 unique feeds to 460 usable feeds after free filters.

Live dynamic calibration on 2026-08-16:

- 700 selected feeds -> 1,717 filtered rolling-24-hour episodes.
- 280 selected feeds -> 872 filtered episodes.
- The production target is 240 feeds, projecting roughly 747 episodes and
  leaving headroom inside the Groq daily tagging budget.
