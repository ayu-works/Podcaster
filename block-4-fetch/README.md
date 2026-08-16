# Block 4 — Fetch and filter

Each run refreshes category discovery, then requests recent episodes in batches
of at most 200 feed IDs. It stamps `run.fetch_cutoff_at` before any outbound
call and starts from the last `ok`/`partial` cutoff, capped at five days.

```bash
cd block-4-fetch
../.venv/bin/python fetch.py --export ../data/episodes-today.csv
```

Before tagging, it drops missing GUIDs, trailers/bonuses, duplicate GUIDs,
descriptions under 100 cleaned characters, and episodes under three minutes.
There is no subscriber history filter here: the episode pool is shared.

All request batches failing raises. Partial failures are counted and successful
batches remain useful.
