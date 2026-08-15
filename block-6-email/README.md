# Block 6: Email

Renders the digest and sends it via Resend.

```bash
cd block-6-email
../.venv/bin/python email_out.py --email you@example.com --dry-run --open
../.venv/bin/python email_out.py --email you@example.com          # sends
```

`email_out.py`, not `email.py` — `email` is in the standard library, and
shadowing it breaks anything that imports it.

## Record first, send second

The digest rows are written **before** the send and marked afterwards. A send
that fails after the API accepted it would otherwise vanish: no record, no way
to tell it from a quiet day, and the same episodes offered again next run.

That gives `digest.kind` four states:

| kind | meaning | counts as already-sent? |
|---|---|---|
| `sent` | delivered | yes |
| `quiet` | nothing cleared the bar | n/a, no items |
| `pending` | written, outcome unknown | **yes** — assume it arrived |
| `failed` | the API rejected it outright | no, offer them again |

`pending` is deliberately conservative. Sending the same episode twice is the
single thing most likely to make this feel broken, so an unknown outcome counts
as delivered. `failed` means Resend definitively refused, so nothing was
delivered and those episodes stay eligible — `fetch.py` filters on
`kind IN ('sent','pending')`.

## A failed run is not a quiet day

If the ranker's call failed, `deliver()` returns `skipped` and writes **no**
digest row. Recording that as `quiet` would corrupt the one metric that tells
you the pipeline is alive, and silence is the product's whole promise
(ARCHITECTURE section 10).

## Email HTML rules

- **Every style inline.** Gmail strips `<style>` blocks — a stylesheet renders
  as unstyled text in the client most people read this in.
- **Tables, not divs.** Outlook renders with Word's engine.
- **No external images.** Blocked by default; a layout depending on them
  arrives broken.
- **Under 102KB.** Gmail clips past that. A two-pick digest is ~7KB.
- **Autoescape on.** Titles come from arbitrary RSS feeds, and a feed that puts
  markup in a title does not get to write HTML into your inbox.

## From-address

`FROM_EMAIL=onboarding@resend.dev` is Resend's shared sender: no DNS setup, but
it will **only** deliver to the address that owns the Resend account. You cannot
use a Gmail address as the sender — Resend only sends from domains you have
proven you own. Sending to anyone else means verifying a real domain.

## Check

Send it to yourself and open it **on your phone**. That is where you will
actually read it, so that is where it has to look right.

## Next

Block 7 wires fetch → rank → deliver into `run.py`, advances `last_run_at`
only on success, and schedules it with launchd.
