# Block 5: The ranker

**This is the product. Everything else is plumbing.**

```bash
cd block-5-rank
../.venv/bin/python rank.py --email you@example.com --runs 3
../.venv/bin/python rank.py --email you@example.com --bar 60   # calibrate
```

## The pool is cut before the call

A run produces ~180 candidates, or ~25k tokens. Groq's free tier allows **8k
tokens per minute**, so one batched call is a 413, not a slow request. The
ranker keeps what fits — about 25 episodes — and the rest are never seen.

The cut in `select_pool()` is **structural, not editorial**: newest first,
round-robin across shows, `RANK_MAX_PER_SHOW` deep. Ordering by keyword overlap
would be cheap and would look smarter, and it would also let a string match
overrule the judgement this block exists to make.

`result.unseen` reports what the limit cost you, every run. Raising `GROQ_TPM`
after a tier upgrade widens the pool with no code change.

## Reasoning model gotcha

`max_completion_tokens` covers the model's *thinking*, not just its answer. A
starved budget does not truncate the reply — it returns a 400
(`json_validate_failed`) with an empty generation. Hence `COMPLETION_TOKENS =
3000`.

## Quiet vs broken

`RankResult.trustworthy` is False when the call failed. Both states send no
email, and without this flag they are indistinguishable — which would make
silence meaningless, and silence is the product's whole promise.
**Block 6 must never record a quiet digest when `trustworthy` is False.**

## What the code enforces, not the prompt

- **The bar.** `RELEVANCE_BAR` is the number most likely to move in week one.
- **PRD rule 6**, never two episodes from one show in a send.

## Check

Run it three times and read the reasons out loud. A reason must name something
concrete from that episode's description. `looks_generic()` flags the failures
automatically — a stock phrase, or no substantial word shared with the
description — and the CLI exits 1 when it fires. **A generic reason is a bug,
not a weak output.**

## Next

Block 6 takes `result.picks` — each a `Pick(episode, score, reason)`.
