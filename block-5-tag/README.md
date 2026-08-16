# Block 5 — Tag and curate

`tag.py` processes the global untagged queue in batches. Groq returns zero to
three allowed topic slugs, a 0–100 score, and a description-grounded why-line.
Malformed or generic output consumes a bounded attempt; daily token exhaustion
leaves untouched work queued for a later run.

`curate.py` is deterministic SQL plus caps. It selects newly tagged episodes at
or above `RELEVANCE_BAR`, rejects stale publication dates, caps one show per
topic, and never pads a quiet topic.

```bash
cd block-5-tag
../.venv/bin/python tag.py --limit 20
../.venv/bin/python curate.py --run-id 1
```
