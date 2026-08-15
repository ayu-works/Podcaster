"""Render the digest and send it (ARCHITECTURE section 6, stage 4).

**The digest rows are written before the send and marked afterwards.** A send
that fails after the API accepted it would otherwise vanish: no record, no way
to tell it from a quiet day, and the same episodes offered again next run.

That gives `digest.kind` four states rather than two:

| kind | meaning | counts as already-sent? |
|---|---|---|
| `sent` | delivered | yes |
| `quiet` | nothing cleared the bar | n/a, has no items |
| `pending` | written, outcome unknown | **yes** — assume it arrived |
| `failed` | the API rejected it outright | no, offer them again |

`pending` is deliberately conservative. Sending the same episode twice is the
single thing most likely to make this feel broken, so an unknown outcome is
treated as delivered.

The module is `email_out.py`, not `email.py`: `email` is in the standard
library and shadowing it breaks any dependency that imports it.
"""

import argparse
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

from _shared import config, db
import fetch as fetch_mod
import rank as rank_mod

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class EmailError(RuntimeError):
    """Delivery could not be attempted or was rejected."""


@dataclass
class Delivery:
    kind: str  # sent | quiet | failed | skipped
    digest_id: int | None = None
    message_id: str = ""
    subject: str = ""
    html: str = ""
    error: str = ""


# --- rendering ---------------------------------------------------------------

# Autoescape is on: titles and descriptions come from arbitrary RSS feeds, and
# a feed that puts markup in an episode title should not get to write HTML into
# the email.
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _pretty_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%-d %b")
    except ValueError:
        return value[:10]


def render(picks: list, scanned: int, shows: int) -> str:
    items = [
        {
            "episode": pick.episode,
            "reason": pick.reason,
            "score": pick.score,
            "duration": rank_mod.format_duration(pick.episode["duration_sec"]),
            "published": _pretty_date(pick.episode["published_at"]),
        }
        for pick in picks
    ]
    return _env.get_template("digest.html").render(
        picks=items,
        scanned=scanned,
        shows=shows,
        sent_on=datetime.now().strftime("%A %-d %B"),
    )


def subject_line(picks: list) -> str:
    """The episode's own title. Nothing summarises it better, and a subject
    that names the thing is what makes an unopened digest worth opening."""
    title = picks[0].episode["title"]
    if len(title) > 68:
        title = title[:67].rstrip() + "…"
    return title + (f"  (+{len(picks) - 1} more)" if len(picks) > 1 else "")


# --- persistence -------------------------------------------------------------


def write_digest(conn, user_id: int, picks: list, kind: str) -> int:
    cursor = conn.execute(
        "INSERT INTO digest (user_id, kind) VALUES (?, ?)", (user_id, kind)
    )
    digest_id = cursor.lastrowid
    if picks:
        conn.executemany(
            "INSERT INTO digest_item (digest_id, episode_id, score, reason_text) "
            "VALUES (?, ?, ?, ?)",
            [(digest_id, p.episode["id"], p.score, p.reason) for p in picks],
        )
    return digest_id


def mark(conn, digest_id: int, kind: str) -> None:
    conn.execute("UPDATE digest SET kind = ? WHERE id = ?", (kind, digest_id))


# --- sending -----------------------------------------------------------------


def send(to: str, subject: str, html: str) -> str:
    if not config.RESEND_API_KEY or config.RESEND_API_KEY.startswith("your_"):
        raise EmailError("RESEND_API_KEY missing from .env; Block 6 cannot send.")
    resend.api_key = config.RESEND_API_KEY
    try:
        response = resend.Emails.send(
            {
                "from": config.FROM_EMAIL,
                "to": [to],
                "subject": subject,
                "html": html,
            }
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a failure
        raise EmailError(f"{type(exc).__name__}: {exc}") from exc
    return (response or {}).get("id", "")


def deliver(conn, user_id: int, result, fetched, dry_run: bool = False) -> Delivery:
    """Record, then send, then mark. Never the other way around."""
    user = conn.execute("SELECT email FROM user WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise EmailError(f"No user with id {user_id}.")

    if not result.picks:
        # A failed ranking call and a genuinely quiet day both produce no picks.
        # Recording the second as the first would corrupt the one metric that
        # tells you the pipeline is alive (ARCHITECTURE section 10).
        if not result.trustworthy:
            return Delivery(kind="skipped", error=result.error or "ranking failed")
        digest_id = None if dry_run else write_digest(conn, user_id, [], "quiet")
        return Delivery(kind="quiet", digest_id=digest_id)

    html = render(result.picks, fetched.after_filter, fetched.shows)
    subject = subject_line(result.picks)
    if dry_run:
        return Delivery(kind="skipped", subject=subject, html=html)

    digest_id = write_digest(conn, user_id, result.picks, "pending")
    conn.commit()  # the record must outlive a crash inside send()

    try:
        message_id = send(user["email"], subject, html)
    except EmailError as exc:
        mark(conn, digest_id, "failed")
        return Delivery(
            kind="failed", digest_id=digest_id, subject=subject, html=html, error=str(exc)
        )

    mark(conn, digest_id, "sent")
    return Delivery(
        kind="sent", digest_id=digest_id, message_id=message_id, subject=subject, html=html
    )


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="render to a file, send nothing"
    )
    parser.add_argument("--open", action="store_true", help="open the dry-run render")
    args = parser.parse_args()

    with db.session() as conn:
        user = conn.execute(
            "SELECT id, email FROM user WHERE email = ?", (args.email,)
        ).fetchone()
        if user is None:
            print(f"No user {args.email}. Subscribe first (Block 3).", file=sys.stderr)
            return 1

        fetched = fetch_mod.fetch_for_user(conn, user["id"])
        print(f"{fetched.raw} raw -> {fetched.after_filter} after filter")

        result = rank_mod.rank(conn, user["id"], fetched.candidates)
        print(
            f"ranked {result.ranked} of {result.candidates}, "
            f"cleared bar {result.cleared_bar}, picks {len(result.picks)}"
        )

        delivery = deliver(conn, user["id"], result, fetched, dry_run=args.dry_run)

    if delivery.kind == "quiet":
        print("quiet — nothing cleared the bar, nothing sent. Recorded as a quiet digest.")
        return 0
    if delivery.kind == "skipped" and not delivery.html:
        print(f"NOT sent and NOT recorded quiet: {delivery.error}", file=sys.stderr)
        return 1
    if delivery.kind == "failed":
        print(f"send failed: {delivery.error}", file=sys.stderr)
        print("digest recorded as 'failed'; those episodes stay eligible.")
        return 1

    for pick in result.picks:
        print(f"  {pick.score:>3}  {pick.episode['show_name'][:44]} — {pick.episode['title'][:44]}")

    if args.dry_run:
        out = config.LOG_DIR / "digest-preview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(delivery.html, encoding="utf-8")
        print(f"\nsubject: {delivery.subject}")
        print(f"{len(delivery.html) / 1024:.1f}KB written to {out}  (Gmail clips at 102KB)")
        if args.open:
            webbrowser.open(out.as_uri())
        return 0

    print(f"\nsent to {user['email']}  (subject: {delivery.subject})")
    print(f"resend id: {delivery.message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
