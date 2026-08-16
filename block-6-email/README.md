# Block 6 — Email

For every active subscriber, SQL joins their topic subscriptions to this run's
shared `daily_pick` rows. Selection deduplicates cross-topic episodes, excludes
that subscriber's pending/sent history, caps one show across the whole email,
and sends nothing when no qualifying picks remain.

```bash
cd block-6-email
../.venv/bin/python email_out.py --run-id 1 --email you@example.com --dry-run --open
```

Delivery commits `sent.status='pending'` before Resend is called. A confirmed
failure becomes `failed` and retries later; an ambiguous pending outcome is not
resent. Each recipient is isolated from the next.

The 600px table template uses inline styles, no external images, HTML escaping,
topic headings, and a tokenized unsubscribe link. Messages also include the
standard one-click unsubscribe headers.
