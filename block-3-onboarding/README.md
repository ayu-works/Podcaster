# Block 3 — Onboarding

The Flask app renders the 20 `config.TOPICS` checkboxes and an email field.
Signup writes only those topic slugs and returns immediately; it never builds a
show list. New and renewed subscribers remain `pending` until they click the
double-opt-in confirmation email.

```bash
cd block-3-onboarding
../.venv/bin/python app.py  # http://127.0.0.1:5001
```

Routes:

- `GET /` — topic form.
- `POST /subscribe` — validate, save topics, send confirmation.
- `GET /confirm/<token>` — idempotently activate.
- `GET /unsubscribe/<token>` — render only; scanners may prefetch it.
- `POST /unsubscribe/<token>` — idempotently unsubscribe.

A hidden honeypot and a small per-IP throttle provide light abuse control.
The repository-root `index.py` exports the app through Vercel's zero-config
Flask entry point, and the root `requirements.txt` supplies its runtime
dependencies. `.python-version` keeps Vercel on Python 3.12, the same runtime
used by GitHub Actions and supported by the pure-Python `turso_serverless`
driver.
