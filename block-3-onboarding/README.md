# Block 3 — Onboarding

The Flask app renders the 20 `config.TOPICS` checkboxes and an email field.
Signup writes only those topic slugs and returns immediately; it never builds a
show list. Signup is single opt-in — new and renewed subscribers are `active`
the moment the form is posted, and a welcome email follows (see
`doc/single-opt-in.md`).

```bash
cd block-3-onboarding
../.venv/bin/python app.py  # http://127.0.0.1:5001
```

Routes:

- `GET /` — topic form.
- `POST /subscribe` — validate, save topics, activate, send welcome email.
- `GET /confirm/<token>` — idempotent; only still activates legacy `pending` rows.
- `GET /unsubscribe/<token>` — render only; scanners may prefetch it.
- `POST /unsubscribe/<token>` — idempotently unsubscribe.

A hidden honeypot and a small per-IP throttle provide light abuse control.
The repository-root `index.py` exports the app through Vercel's zero-config
Flask entry point, and the root `requirements.txt` supplies its runtime
dependencies. `.python-version` keeps Vercel on Python 3.12, the same runtime
used by GitHub Actions and supported by the pure-Python `turso_serverless`
driver.
