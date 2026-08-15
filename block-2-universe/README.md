# Block 2: Podcast Index client + the universe

Builds the ~200-show candidate list. **That list is the ceiling on everything
Podcaster will ever recommend you**, so the check here is reading it by hand.

## Files

| File | What it does |
|---|---|
| `podcastindex.py` | API client. SHA1 auth, `search_shows()`, `episodes_by_feed()`. Raises on failure — a silent zero-candidate run is the worst outcome. |
| `universe.py` | Uses Groq to expand interests, searches every term, filters and dedupes feeds, keeps the top N, and replaces the user's `candidate_show` set. |
| `interests.example.toml` | Template. Copy to `../interests.toml` and edit. |
| `_shared.py` | Import shim for Block 1's `config` and `db`. |

## Run it

```bash
cd block-2-universe
cp interests.example.toml ../interests.toml   # then edit it

../.venv/bin/python podcastindex.py "AI engineering"      # auth smoke test
../.venv/bin/python universe.py --dry-run                 # THE CHECK
../.venv/bin/python universe.py --email you@example.com   # writes the rows
```

Groq generates 18 focused search terms per interest by default. Live testing
showed that smaller expansions could not leave 200 fresh feeds for a
three-interest profile after filtering. Optional
terms in `interests.toml` are only a debugging fallback; use them with
`--use-file-terms` when testing without Groq.

## Check

Read the show names. If the list looks generic or off-topic, the search terms
are wrong — fix them and rerun **before** writing another line of code. A bad
universe fails invisibly and no amount of prompt tuning later will rescue it.

## Writing search terms — measured behaviour

`/search/byterm` matches show **titles and descriptions**, so it rewards terms
that sound like something a show would call itself. Descriptive phrases return
almost nothing. Measured on the example file:

| Term | Results |
|---|---|
| `AI engineering` | 40 |
| `B2B sales` | 40 |
| `Indian economy` | 13 |
| `AI in production` | 9 |
| `AI developer tools` | **0** |

So: **1–2 words, phrased as a category or a show name.** Not a sentence.

Two more findings that change the plan's arithmetic:

- **~70% of the catalogue is stale.** Of 378 unique feeds found, 263 had
  published nothing in 60 days. Expect to lose two thirds of every search.
- **Term overlap is near zero.** 374 of 378 feeds matched exactly one term.
  The reciprocal-rank fusion in `rank_feeds()` still works, but in practice
  ordering is driven by position within a single search, not by feeds being
  central to several interests.

Each term requests the top 40 results. Deeper pages added mostly noisy tail
results in live testing.

## Free quality filters

`/search/byterm` returns `language`, `dead`, `episodeCount` and `newestItemPubdate`
alongside each feed, so `rank_feeds()` filters on all four at zero API cost. They
judge *usability*, not relevance — relevance is the ranker's job in Block 5.

Measured on the real interests file: 1641 unique feeds → 1127 stale, 41 wrong
language, 13 under 5 episodes → **460 usable** for a 200-slot universe. That
headroom matters: it means tightening a bad search term promotes a better show
into the freed slot rather than leaving a hole.

## Ambiguous terms are the main source of junk

Single common words collide across domains, and the junk lands in the tail:

| Term | What it dragged in |
|---|---|
| `kitchen` | Pickleball ("the kitchen" is a court zone), *Kitchen Sink WordPress* |
| `eating` | *Eat Em Up: A Detroit Tigers Podcast*, endurance-sports nutrition |
| `creative` | Generic business and marketing shows |
| `productivity` | Time-management self-help |

Replacing those four with `meal prep`, `food writing`, `art direction` and
`business automation` cleaned the bottom ~25 slots. The Groq expansion prompt
therefore rejects generic standalone terms.

## Watch for

**Content-farm networks.** Some publishers spam the index with dozens of
near-identical shows. A deliberately short title blocklist removes the two
networks observed in live testing; normalized-title deduplication handles
duplicate catalogue entries without merging genuinely different shows.

## Next

Block 3 (onboarding) calls `universe.build(conn, user_id, interests)` directly
from `POST /subscribe`. The signature is already shaped for that.
