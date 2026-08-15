# Block 4: Fetch and filter

Polls the user's ~200 shows for new episodes and cuts them down to what the
ranker should see. No LLM anywhere in this block.

## Files

| File | What it does |
|---|---|
| `fetch.py` | Both stages: poll the feeds, upsert on `guid`, then apply the four filter rules. |
| `_shared.py` | Import shim for Block 1's `config`/`db` and Block 2's `podcastindex`. |

## Run it

```bash
cd block-4-fetch
../.venv/bin/python fetch.py --email you@example.com
../.venv/bin/python fetch.py --email you@example.com --days 2   # override the window
```

## The window is `last_run_at`, not "the last two days"

A fixed window silently drops episodes whenever a run fails or the laptop was
asleep. Using the last successful run means a missed Wednesday gets picked up on
Friday, so a skipped run is harmless rather than lossy. `MAX_LOOKBACK_DAYS`
caps it, so a two-week gap produces a digest instead of a flood.

`last_run_at` is written by SQLite's `datetime('now')` — UTC, with no timezone
marker on it. `since_timestamp()` attaches UTC explicitly. Parsing it as naive
local time would shift the window by hours and quietly lose or repeat episodes,
which is the kind of bug you find in week three.

**This block does not advance `last_run_at`.** That belongs to the run job,
after a successful delivery. Fetching and then failing must not consume the
window.

## What gets dropped, and where

| Rule | Where | Why there |
|---|---|---|
| No `guid` | before the upsert | Nothing to dedupe on. |
| `episodeType` is trailer or bonus | before the upsert | Useless to *every* user, so it does not belong in a cache shared by all of them. |
| Already sent to this user | filter | ARCHITECTURE section 5: the LLM is never responsible for remembering. |
| Duplicate `guid` | filter | Dedupe on guid, **never** on title — feeds republish with edited titles constantly. |
| Description under `MIN_DESC_CHARS` | filter | The ranker cannot judge what it cannot read. This rule does the most work. |
| Under `MIN_EPISODE_SEC` | filter | Stings and promos. |

Descriptions are stripped of HTML and whitespace-collapsed **before** the length
check. This matters more than it looks: `<p>Short one.</p><a href="…">link</a>`
is 82 characters of markup and 15 characters of prose. Left raw, it clears a
100-character bar on tags alone and then eats the ranker's 400-character budget
carrying no signal.

An unknown duration is not treated as a short episode. Trailers are caught by
type, so a missing `duration` field costs a real episode nothing.

## Failure handling

One dead feed is counted, not raised — 199 good shows should still produce a
digest. **Every** feed failing does raise. That is a bad key or no network, and
it is indistinguishable from a quiet week unless it makes noise
(ARCHITECTURE section 10).

## Check

```
191 raw -> 180 after filter
  -    7  under 3 minutes
  -    2  description under 100 chars
```

You want 60+. **Under 30, stop** — the CLI exits 1 and says so. A thin pool
produces bad picks that look exactly like a bad ranker, and you will lose an
hour debugging Block 5 instead of Block 2.

### The check the plan does not make

A healthy count is not a healthy pool. The count only catches a universe that is
too *narrow*; it says nothing about one that is off-topic. Read the titles the
CLI prints, and if they are not about your interests, the bug is in Block 2's
search terms — nothing in this block or the next one can recover from it.

## Next

Block 5 (`rank.py`) takes `fetch_for_user(conn, user_id).candidates` — rows
straight from `episode`, newest first — and picks at most
`PICKS_PER_EMAIL` of them.
