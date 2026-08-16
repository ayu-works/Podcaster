"""Immediate topic signup, double opt-in, and scanner-safe unsubscribe."""

from __future__ import annotations

import argparse
import re
import secrets
import threading
import time
from collections import defaultdict, deque

import resend
from flask import Flask, render_template, request, url_for

from _shared import config, db

app = Flask(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_SIGNUPS: dict[str, deque[float]] = defaultdict(deque)
_SIGNUPS_LOCK = threading.Lock()


class ConfirmationEmailError(RuntimeError):
    """The double-opt-in message could not be handed to Resend."""


def _signup_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    return forwarded or request.remote_addr or "unknown"


def rate_limited(ip: str, now: float | None = None) -> bool:
    """Small in-process brake; deployment-level controls can replace it later."""
    now = time.monotonic() if now is None else now
    cutoff = now - config.SIGNUP_RATE_WINDOW_SEC
    with _SIGNUPS_LOCK:
        attempts = _SIGNUPS[ip]
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= config.SIGNUP_RATE_LIMIT:
            return True
        attempts.append(now)
        return False


def confirmation_url(token: str) -> str:
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/confirm/{token}"


def send_confirmation(email: str, token: str) -> str:
    if not config.RESEND_API_KEY or config.RESEND_API_KEY.startswith("your_"):
        raise ConfirmationEmailError("RESEND_API_KEY missing from .env")
    resend.api_key = config.RESEND_API_KEY
    link = confirmation_url(token)
    try:
        response = resend.Emails.send(
            {
                "from": config.FROM_EMAIL,
                "to": [email],
                "subject": "Confirm your Podcaster subscription",
                "html": (
                    "<p>Confirm that you want Podcaster's curated episode digest.</p>"
                    f'<p><a href="{link}">Confirm my subscription</a></p>'
                    "<p>If you did not request this, ignore this email.</p>"
                ),
            }
        )
    except Exception as exc:
        raise ConfirmationEmailError(f"{type(exc).__name__}: {exc}") from exc
    if isinstance(response, dict):
        return response.get("id", "")
    return getattr(response, "id", "") or ""


def _selected_topics() -> tuple[list[str], str]:
    submitted = request.form.getlist("topic")
    unknown = sorted(set(submitted) - set(config.TOPIC_SLUGS))
    if unknown:
        return [], "Choose topics from the list shown on this page."
    selected_set = set(submitted)
    selected = [slug for slug in config.TOPIC_SLUGS if slug in selected_set]
    if not selected:
        return [], "Pick at least one topic."
    return selected, ""


def _save_signup(conn, email: str, topics: list[str]) -> tuple[str, bool]:
    """Return confirmation token and whether a message must be sent."""
    row = conn.execute(
        "SELECT id, status, confirm_token FROM subscriber WHERE email=?", (email,)
    ).fetchone()
    if row is None:
        confirm_token = secrets.token_urlsafe(32)
        unsub_token = secrets.token_urlsafe(32)
        while unsub_token == confirm_token:  # defensive; practically impossible
            unsub_token = secrets.token_urlsafe(32)
        subscriber_id = conn.execute(
            "INSERT INTO subscriber (email, unsub_token, confirm_token, status) "
            "VALUES (?, ?, ?, 'pending')",
            (email, unsub_token, confirm_token),
        ).lastrowid
        needs_confirmation = True
    else:
        subscriber_id = row["id"]
        confirm_token = row["confirm_token"]
        needs_confirmation = row["status"] != "active"
        if row["status"] in ("paused", "unsubscribed"):
            confirm_token = secrets.token_urlsafe(32)
            unsub_token = secrets.token_urlsafe(32)
            while unsub_token == confirm_token:
                unsub_token = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE subscriber SET status='pending', confirmed_at=NULL, "
                "confirm_token=?, unsub_token=? WHERE id=?",
                (confirm_token, unsub_token, subscriber_id),
            )

    conn.execute("DELETE FROM subscription WHERE subscriber_id=?", (subscriber_id,))
    conn.executemany(
        "INSERT INTO subscription (subscriber_id, topic) VALUES (?, ?)",
        [(subscriber_id, topic) for topic in topics],
    )
    return confirm_token, needs_confirmation


@app.get("/")
def onboard():
    return render_template(
        "onboard.html",
        topics=config.TOPICS,
        missing_keys=config.missing_keys(6),
        form={},
        error=None,
    )


@app.post("/subscribe")
def subscribe():
    # Bots commonly fill every input. Return the ordinary success page so the
    # field does not reveal itself, but create no row and send no message.
    if request.form.get("company", "").strip():
        return render_template(
            "message.html",
            title="Check your inbox",
            message="A confirmation link is on its way. It must be clicked before any digest is sent.",
        )

    if rate_limited(_signup_ip()):
        return render_template(
            "message.html",
            title="Please try again later",
            message="There have been too many signup attempts from this connection.",
        ), 429

    email = request.form.get("email", "").strip().lower()
    topics, topic_error = _selected_topics()
    error = "" if EMAIL_RE.fullmatch(email) else "That email address does not look right."
    error = error or topic_error
    if error:
        return (
            render_template(
                "onboard.html",
                topics=config.TOPICS,
                missing_keys=config.missing_keys(6),
                form={"email": email, "topics": topics},
                error=error,
            ),
            400,
        )

    with db.session() as conn:
        confirm_token, needs_confirmation = _save_signup(conn, email, topics)

    if needs_confirmation:
        try:
            send_confirmation(email, confirm_token)
        except ConfirmationEmailError:
            app.logger.exception("confirmation email failed for %s", email)
            return render_template(
                "message.html",
                title="Saved, but email is delayed",
                message="Your topics are saved. Please try subscribing again in a few minutes to resend confirmation.",
            ), 503
        title = "Check your inbox"
        message = "Click the confirmation link before any podcast digest can be sent."
    else:
        title = "Preferences updated"
        message = "Your new topic selection will be used for the next digest."
    return render_template("message.html", title=title, message=message)


@app.get("/confirm/<token>")
def confirm(token: str):
    with db.session() as conn:
        conn.execute(
            "UPDATE subscriber SET status='active', "
            "confirmed_at=COALESCE(confirmed_at, datetime('now')) "
            "WHERE confirm_token=? AND status IN ('pending', 'active')",
            (token,),
        )
    return render_template(
        "message.html",
        title="Subscription confirmed",
        message="You're ready. The next strong episodes matching your topics will arrive by email.",
    )


@app.get("/unsubscribe/<token>")
def unsubscribe_prompt(token: str):
    # GET is deliberately read-only: mail security scanners prefetch links.
    return render_template("unsubscribe.html", token=token, complete=False)


@app.post("/unsubscribe/<token>")
def unsubscribe(token: str):
    with db.session() as conn:
        conn.execute(
            "UPDATE subscriber SET status='unsubscribed' WHERE unsub_token=?",
            (token,),
        )
    # Identical response for valid, already-used, and unknown tokens.
    return render_template("unsubscribe.html", token=token, complete=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    db.init_db()
    print(f"onboarding at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
