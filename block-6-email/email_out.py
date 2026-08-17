"""Render and deliver this run's shared picks to active subscribers."""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

from _shared import config, db, links

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """A recipient's delivery attempt was rejected."""


@dataclass
class Delivery:
    subscriber_id: int
    email: str
    kind: str  # sent | failed | skipped | preview
    episode_ids: list[int] = field(default_factory=list)
    message_id: str = ""
    subject: str = ""
    html: str = ""
    error: str = ""


@dataclass
class DeliveryResult:
    deliveries: list[Delivery] = field(default_factory=list)

    @property
    def sent(self) -> int:
        return sum(item.kind == "sent" for item in self.deliveries)

    @property
    def failed(self) -> int:
        return sum(item.kind == "failed" for item in self.deliveries)

    @property
    def skipped(self) -> int:
        return sum(item.kind == "skipped" for item in self.deliveries)


_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "?"
    return f"{round(seconds / 60)}m"


def _pretty_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%-d %b")
    except ValueError:
        return value[:10]


def load_picks(
    conn,
    subscriber_id: int,
    run_id: int,
    max_picks: int | None = None,
) -> list[dict]:
    """Merge subscribed topics, dedupe, and enforce whole-email caps."""
    rows = conn.execute(
        """
        SELECT dp.topic, dp.rank, e.*
        FROM subscription sub
        JOIN daily_pick dp ON dp.topic = sub.topic AND dp.run_id = ?
        JOIN episode e ON e.id = dp.episode_id
        LEFT JOIN sent s
          ON s.subscriber_id = sub.subscriber_id AND s.episode_id = e.id
        WHERE sub.subscriber_id = ?
          AND (s.status IS NULL OR s.status = 'failed')
        ORDER BY e.score DESC, dp.rank, e.published_at DESC
        """,
        (run_id, subscriber_id),
    ).fetchall()

    pick_limit = config.MAX_PER_EMAIL if max_picks is None else max_picks
    if pick_limit < 1:
        raise ValueError("max_picks must be positive")
    selected: list[dict] = []
    seen_episodes: set[int] = set()
    show_counts: Counter = Counter()
    for row in rows:
        if row["id"] in seen_episodes:
            continue
        if show_counts[row["feed_id"]] >= config.MAX_PER_SHOW_PER_EMAIL:
            continue
        seen_episodes.add(row["id"])
        show_counts[row["feed_id"]] += 1
        selected.append(dict(row))
        if len(selected) == pick_limit:
            break
    return selected


def unsubscribe_url(token: str) -> str:
    return links.unsubscribe_url(token)


def render(picks: list[dict], token: str) -> str:
    groups: list[dict] = []
    by_topic: dict[str, list[dict]] = {}
    labels = dict(config.TOPICS)
    for pick in picks:
        item = {
            "episode": pick,
            "reason": pick["why"],
            "duration": format_duration(pick["duration_sec"]),
            "published": _pretty_date(pick["published_at"]),
            # The last guard before an irreversible send. `web_url` is already
            # a page URL at ingest, but rows predating that fix hold enclosure
            # URLs, and no digest may link a listener to a bare audio file.
            # Empty means the template prints the title with no link and no
            # button, which is the correct degradation.
            "listen_url": links.safe_page_url(pick["web_url"]),
        }
        by_topic.setdefault(pick["topic"], []).append(item)
    for slug, items in by_topic.items():
        groups.append({"slug": slug, "label": labels[slug], "items": items})
    return _env.get_template("digest.html").render(
        groups=groups,
        pick_count=len(picks),
        sent_on=datetime.now().strftime("%A %-d %B"),
        unsubscribe_url=unsubscribe_url(token),
    )


def subject_line(picks: list[dict]) -> str:
    title = picks[0]["title"]
    if len(title) > 68:
        title = title[:67].rstrip() + "…"
    return title + (f"  (+{len(picks) - 1} more)" if len(picks) > 1 else "")


def send(to: str, subject: str, html: str, token: str) -> str:
    if not config.RESEND_API_KEY or config.RESEND_API_KEY.startswith("your_"):
        raise EmailError("RESEND_API_KEY missing from .env")
    if config.localhost_base_url():
        logger.warning("PUBLIC_BASE_URL is a localhost address; digest links will be dead in the wild")
    url = unsubscribe_url(token)
    resend.api_key = config.RESEND_API_KEY
    try:
        response = resend.Emails.send(
            {
                "from": config.FROM_EMAIL,
                "to": [to],
                "subject": subject,
                "html": html,
                "headers": {
                    "List-Unsubscribe": f"<{url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
            }
        )
    except Exception as exc:
        raise EmailError(f"{type(exc).__name__}: {exc}") from exc
    if isinstance(response, dict):
        return response.get("id", "")
    return getattr(response, "id", "") or ""


def _record_pending(conn, subscriber_id: int, run_id: int, episode_ids: list[int]) -> None:
    conn.executemany(
        """
        INSERT INTO sent (subscriber_id, episode_id, run_id, status, attempts)
        VALUES (?, ?, ?, 'pending', 1)
        ON CONFLICT (subscriber_id, episode_id) DO UPDATE SET
            run_id = excluded.run_id,
            status = 'pending',
            attempts = sent.attempts + 1,
            last_error = NULL,
            sent_at = NULL
        """,
        [(subscriber_id, episode_id, run_id) for episode_id in episode_ids],
    )
    conn.commit()


def _mark(conn, subscriber_id: int, episode_ids: list[int], status: str, error: str | None = None):
    conn.executemany(
        "UPDATE sent SET status=?, last_error=?, "
        "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE NULL END "
        "WHERE subscriber_id=? AND episode_id=?",
        [
            (status, error, status, subscriber_id, episode_id)
            for episode_id in episode_ids
        ],
    )
    conn.commit()


def deliver_subscriber(
    conn,
    subscriber,
    run_id: int,
    dry_run: bool = False,
    max_picks: int | None = None,
    min_picks: int | None = None,
) -> Delivery:
    picks = load_picks(conn, subscriber["id"], run_id, max_picks=max_picks)
    if min_picks is not None and len(picks) < min_picks:
        return Delivery(
            subscriber["id"],
            subscriber["email"],
            "skipped",
            error=f"requires {min_picks} picks but found {len(picks)}",
        )
    if not picks:
        return Delivery(subscriber["id"], subscriber["email"], "skipped")

    html = render(picks, subscriber["unsub_token"])
    subject = subject_line(picks)
    episode_ids = [pick["id"] for pick in picks]
    if dry_run:
        return Delivery(
            subscriber["id"], subscriber["email"], "preview",
            episode_ids, subject=subject, html=html,
        )

    _record_pending(conn, subscriber["id"], run_id, episode_ids)
    try:
        message_id = send(
            subscriber["email"], subject, html, subscriber["unsub_token"]
        )
    except EmailError as exc:
        db.ensure_connection(conn)
        _mark(conn, subscriber["id"], episode_ids, "failed", str(exc))
        return Delivery(
            subscriber["id"], subscriber["email"], "failed", episode_ids,
            subject=subject, html=html, error=str(exc),
        )

    # Resend is external and the pending marker was committed before it. Use
    # a harmless read to refresh an expired Turso stream before recording the
    # final outcome.
    db.ensure_connection(conn)
    _mark(conn, subscriber["id"], episode_ids, "sent")
    return Delivery(
        subscriber["id"], subscriber["email"], "sent", episode_ids,
        message_id=message_id, subject=subject, html=html,
    )


def deliver_all(
    conn,
    run_id: int,
    dry_run: bool = False,
    email: str | None = None,
    max_picks: int | None = None,
    min_picks: int | None = None,
) -> DeliveryResult:
    sql = (
        "SELECT id, email, unsub_token FROM subscriber "
        "WHERE status='active'"
    )
    parameters = ()
    if email:
        sql += " AND email=?"
        parameters = (email.strip().lower(),)
    subscribers = conn.execute(sql + " ORDER BY id", parameters).fetchall()
    result = DeliveryResult()
    for subscriber in subscribers:
        # External send errors become a failed Delivery inside
        # deliver_subscriber. This outer guard also isolates an unexpected
        # recipient-specific render or database problem from later recipients.
        try:
            db.ensure_connection(conn)
            delivery = deliver_subscriber(
                conn,
                subscriber,
                run_id,
                dry_run=dry_run,
                max_picks=max_picks,
                min_picks=min_picks,
            )
        except Exception as exc:  # noqa: BLE001 -- isolation is the contract
            try:
                conn.rollback()
            except Exception:
                pass
            delivery = Delivery(
                subscriber["id"], subscriber["email"], "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        result.deliveries.append(delivery)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--email")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    with db.session() as conn:
        run_id = args.run_id
        if run_id is None:
            row = conn.execute("SELECT id FROM run ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                print("No run exists", file=sys.stderr)
                return 1
            run_id = row["id"]
        if args.email:
            subscribers = conn.execute(
                "SELECT id, email, unsub_token FROM subscriber "
                "WHERE email=? AND status='active'",
                (args.email,),
            ).fetchall()
            result = DeliveryResult(
                [deliver_subscriber(conn, row, run_id, args.dry_run) for row in subscribers]
            )
        else:
            result = deliver_all(conn, run_id, args.dry_run)

    print(
        f"run {run_id}: sent {result.sent}, failed {result.failed}, "
        f"skipped {result.skipped}, previews "
        f"{sum(item.kind == 'preview' for item in result.deliveries)}"
    )
    previews = [item for item in result.deliveries if item.kind == "preview"]
    if previews:
        out = config.LOG_DIR / "digest-preview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(previews[0].html, encoding="utf-8")
        print(f"preview: {out}")
        if args.open:
            webbrowser.open(out.as_uri())
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
