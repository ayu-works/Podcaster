# Block 7 — Run

Executes the dynamic pipeline in order:

```text
recent category discovery -> batched episode fetch -> tag -> curate -> email
```

Run locally with `../.venv/bin/python run.py --dry-run`. The production
schedule lives in `.github/workflows/run.yml`. A static monthly seed is not
scheduled: recent category discovery is refreshed on every run.
