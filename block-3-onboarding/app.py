"""Single opt-in topic signup, welcome email, and scanner-safe unsubscribe."""

from __future__ import annotations

import argparse
import re
import secrets
import threading
import time
from collections import defaultdict, deque

import resend
from flask import Flask, render_template, request, url_for

from _shared import config, db, links

app = Flask(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_SIGNUPS: dict[str, deque[float]] = defaultdict(deque)
_SIGNUPS_LOCK = threading.Lock()

# Shared between the honeypot branch and a genuine signup so the two are
# byte-identical; that indistinguishability is the whole point of the trap.
SUBSCRIBED_TITLE = "You're subscribed"
SUBSCRIBED_BODY = (
    "Your first digest arrives on the next run — up to ten episodes, "
    "and only the ones that earn it."
)


class WelcomeEmailError(RuntimeError):
    """The welcome message could not be handed to Resend."""


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


def _new_tokens() -> tuple[str, str]:
    """Distinct confirm and unsubscribe tokens."""
    confirm_token = secrets.token_urlsafe(32)
    unsub_token = secrets.token_urlsafe(32)
    while unsub_token == confirm_token:  # defensive; practically impossible
        unsub_token = secrets.token_urlsafe(32)
    return confirm_token, unsub_token


def send_welcome(email: str, topics: list[str], unsub_token: str) -> str:
    if not config.RESEND_API_KEY or config.RESEND_API_KEY.startswith("your_"):
        raise WelcomeEmailError("RESEND_API_KEY missing from .env")
    if config.localhost_base_url():
        app.logger.warning(
            "PUBLIC_BASE_URL is a localhost address; welcome email links will be dead in the wild"
        )
    resend.api_key = config.RESEND_API_KEY
    labels = dict(config.TOPICS)
    context = {
        "topics": [labels[slug] for slug in topics],
        "unsubscribe_url": links.unsubscribe_url(unsub_token),
        "not_me_url": links.unsubscribe_url(unsub_token) + "?not-me=1",
        "site_url": config.PUBLIC_BASE_URL.rstrip("/"),
    }
    try:
        response = resend.Emails.send(
            {
                "from": config.FROM_EMAIL,
                "to": [email],
                "subject": "You're subscribed to Podcaster",
                "html": render_template("welcome_email.html", **context),
                # A text/plain alternative is one of the cheapest real
                # spam-score wins, and nothing this project sends has one yet.
                "text": render_template("welcome_email.txt", **context),
                "headers": {
                    "List-Unsubscribe": f"<{context['unsubscribe_url']}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            }
        )
    except Exception as exc:
        raise WelcomeEmailError(f"{type(exc).__name__}: {exc}") from exc
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
    """Return the unsubscribe token and whether a welcome mail must be sent.

    Signup is single opt-in: the row is active the moment the form is posted.
    `confirm_token` is still issued because the column is NOT NULL UNIQUE and
    there is no migration path, and because /confirm stays alive for links
    already sitting in inboxes.
    """
    row = conn.execute(
        "SELECT id, status, unsub_token FROM subscriber WHERE email=?", (email,)
    ).fetchone()
    if row is None:
        confirm_token, unsub_token = _new_tokens()
        subscriber_id = conn.execute(
            "INSERT INTO subscriber (email, unsub_token, confirm_token, status, "
            "confirmed_at) VALUES (?, ?, ?, 'active', datetime('now'))",
            (email, unsub_token, confirm_token),
        ).lastrowid
        needs_welcome = True
    else:
        subscriber_id = row["id"]
        unsub_token = row["unsub_token"]
        needs_welcome = row["status"] != "active"
        if needs_welcome:
            # Returning from unsubscribed/paused, or a legacy pending row.
            # Rotating unsub_token retires the link in any digest already sent,
            # which is the existing behaviour and the correct one.
            confirm_token, unsub_token = _new_tokens()
            conn.execute(
                "UPDATE subscriber SET status='active', "
                "confirmed_at=COALESCE(confirmed_at, datetime('now')), "
                "confirm_token=?, unsub_token=? WHERE id=?",
                (confirm_token, unsub_token, subscriber_id),
            )

    conn.execute("DELETE FROM subscription WHERE subscriber_id=?", (subscriber_id,))
    conn.executemany(
        "INSERT INTO subscription (subscriber_id, topic) VALUES (?, ?)",
        [(subscriber_id, topic) for topic in topics],
    )
    return unsub_token, needs_welcome


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
            title=SUBSCRIBED_TITLE,
            message=SUBSCRIBED_BODY,
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
        unsub_token, needs_welcome = _save_signup(conn, email, topics)

    if needs_welcome:
        try:
            send_welcome(email, topics, unsub_token)
        except WelcomeEmailError:
            # The subscription is already live; the welcome note is not
            # load-bearing. Log it and let the person get on with their day.
            app.logger.exception("welcome email failed for %s", email)
        title = SUBSCRIBED_TITLE
        message = SUBSCRIBED_BODY
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
        title="You're all set",
        message="This address is subscribed. The next strong episodes matching your topics will arrive by email.",
    )


@app.get("/unsubscribe/<token>")
def unsubscribe_prompt(token: str):
    # GET is deliberately read-only: mail security scanners prefetch links.
    return render_template(
        "unsubscribe.html",
        token=token,
        complete=False,
        not_me=request.args.get("not-me") == "1",
    )


@app.post("/unsubscribe/<token>")
def unsubscribe(token: str):
    with db.session() as conn:
        conn.execute(
            "UPDATE subscriber SET status='unsubscribed' WHERE unsub_token=?",
            (token,),
        )
    # Identical response for valid, already-used, and unknown tokens.
    return render_template("unsubscribe.html", token=token, complete=True, not_me=False)


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
