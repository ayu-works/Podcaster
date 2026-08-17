# Why signup went single opt-in

`POST /subscribe` used to write `status='pending'` and do nothing else until
the person went to their inbox and clicked `GET /confirm/<token>`. That
second click was the entire mechanism by which a signup became real. This
document records why that mechanism was removed, not just what replaced it.

## The drop-off is a real cost, the confirmation mail was not

Every step between form submission and an active subscriber loses people —
that is true of any double opt-in flow. Here the cost was worse than usual:
the confirmation mail itself was three bare `<p>` tags built as an f-string
(`app.py:62-66` before this change), with no branding, no explanation of what
Podcaster sends, and no `List-Unsubscribe` header. It asked for a second
action from the recipient while giving them nothing that made the product
look real. A visitor who filled the form and got that email had little reason
to trust the link enough to click it.

Meanwhile the reason double opt-in existed — stopping someone from typing a
stranger's address into the form — was only partially served by it anyway,
since nothing stopped the *first* mail (the bare confirmation request) from
reaching that stranger regardless.

## The tradeoff we knowingly accepted

Single opt-in means anyone can enter an address that is not theirs and it
starts receiving mail immediately. `doc/ARCHITECTURE.md` used to cite this
exact risk as the reason `pending` existed. We are not pretending the risk
disappeared; we decided it is acceptable for this project's shape:

- This is a low-frequency (four mornings a week), unpaid, single-purpose
  digest, not a marketing list being sold or monetized. The blast radius of
  misuse is small.
- The standard newsletter tradeoff (subscribe = subscribed) is normal and
  legal in the US. It is weaker footing under GDPR/CASL, where proof of
  consent matters more; that risk is accepted here and would need revisiting
  before any EU-facing launch.
- The mitigations that used to live entirely in the second click now live
  elsewhere, and in combination they are not nothing:
  - **Honeypot** (`app.py`, the hidden `company` field) still silently drops
    the largest, laziest class of abuse — bots that fill every input.
  - **Per-IP throttle** (`SIGNUP_RATE_LIMIT` / `SIGNUP_RATE_WINDOW_SEC`) still
    caps how many addresses one source can enroll per hour.
  - **The welcome email carries a deliberate escape hatch.** It states plainly
    that this address was entered on Podcaster, and the "Didn't sign up?
    Remove this address" link is set in body-size accent type, separated from
    the ordinary muted unsubscribe link below it. It is louder than a standard
    footer unsubscribe on purpose: for someone who did not sign themselves up,
    it is the only thing between them and recurring mail. One click, no login,
    no reply.
  - **`List-Unsubscribe` / `List-Unsubscribe-Post: One-Click` headers** are
    now sent on the welcome mail, so mail clients surface a one-click unsubscribe
    control directly in their UI, not just inside the message body.

The net effect, stated honestly: a wrongly-subscribed person who acts on the
welcome mail gets exactly one message. One who ignores it keeps receiving
digests until they unsubscribe — that is the part `pending` used to prevent
and nothing here fully replaces. What the welcome mail buys is that the very
first message they receive is branded, explains itself, and hands them a
prominent one-click exit, instead of a bare confirmation request. Under
double opt-in they would instead have received a confirmation request that,
if ignored, left them alone forever — but if *clicked* by mistake (people
click things), enrolled them the same way, with a worse first impression and
no unsubscribe header at all.

## Why `confirm_token` and `/confirm` are still here

`db.init_db()` is `CREATE TABLE IF NOT EXISTS` only; there is no migration
system and no safe way to drop a `NOT NULL UNIQUE` column from a live Turso
database. So:

- `subscriber.confirm_token` is still generated on every signup, even though
  nothing reads it to gate activation any more.
- `GET /confirm/<token>` stays mounted and idempotent. It is a no-op for a
  single-opt-in signup (the row is already `active`), but it still promotes
  any legacy `pending` row to `active` for confirmation links that are
  already sitting in real inboxes from before this change shipped. Deleting
  the route would turn those old links into dead ends.

Both are vestigial by design, not oversight.

## Why the welcome email has no `<img>` tag

Gmail and Outlook block remote images by default, so a welcome mail built
around a hero image or logo renders broken more often than not — a worse
first impression than sending nothing. Remote image loads are also a
well-known spam-scoring signal, and base64/CID inlining is unreliable enough
in Gmail specifically that it is not a safe substitute. The welcome template
gets its visual identity entirely from color, type scale, emoji, and
table-based layout instead — everything renders identically whether or not
images are enabled, in every client. `doc/ARCHITECTURE.md` already applies
this same no-external-images rule to the digest; the welcome mail keeps it
consistent rather than reintroducing the risk in the one place a new
subscriber's first impression is formed.

See `doc/ARCHITECTURE.md` §7 and §8 for the current (single opt-in) behavior
this document explains the reasoning behind.
