# Block 7 — Run

Executes the dynamic pipeline in order:

```text
recent category discovery -> batched episode fetch -> tag -> curate -> email
```

Run locally with `../.venv/bin/python run.py --dry-run`. The production
schedule lives in `.github/workflows/run.yml`. A static monthly seed is not
scheduled: recent category discovery is refreshed on every run.

Manual Actions runs default to dry-run for safety. `tag_limit` bounds only a
manually requested smoke test (for example, 100 episodes); scheduled runs omit
the flag and continue to process the full shared queue. The optional manual
`recipient` input restricts delivery to one exact active subscriber; scheduled
runs leave it empty and deliver to all active subscribers.
