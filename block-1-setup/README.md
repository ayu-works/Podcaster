# Block 1 — Setup

`config.py` owns every topic, category mapping, limit, path, and environment
value. `db.py` owns the nine-table shared-pool schema and opens local SQLite or
the remote Rust-based Turso Database without changing downstream SQL. Hosted
connections use Turso's official `turso_serverless` DB-API driver.

```bash
cd block-1-setup
../.venv/bin/python db.py
../.venv/bin/python check.py
```

The nine tables are `subscriber`, `subscription`, `show`, `show_topic`,
`episode`, `episode_topic`, `run`, `daily_pick`, and `sent`.

Local development falls back to `podcaster.db`. GitHub Actions must use
`DATABASE_URL` and `DATABASE_TOKEN`; a runner-local database disappears after
the job.
