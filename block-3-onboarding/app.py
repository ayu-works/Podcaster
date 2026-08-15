"""Onboarding: the form, and the universe build it kicks off.

ARCHITECTURE section 8. Two routes carry the flow — `GET /` renders the form,
`POST /subscribe` creates the user and starts the build. The build takes ~30
seconds, so it runs in a thread and `/done/<job>` polls `/status/<job>` rather
than leaving a blank loading page.

The design rule that matters here: **the free text is the signal.** It goes
into the ranking prompt word for word and into the search expansion. The chips
exist only to beat the blank page, so clicking one seeds an editable line in
the text box instead of becoming an interest in its own right. A chip label
only ever reaches the database when the user wrote nothing at all — a floor,
not a feature.
"""

import argparse
import re
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field

from flask import Flask, jsonify, redirect, render_template, request, url_for

from _shared import config, db  # noqa: F401  (sets sys.path for the import below)
import universe

app = Flask(__name__)

# ~20 coarse areas. Deliberately broad: they are a prompt for the text box, not
# a taxonomy. See the module docstring for why they are not interests.
CHIPS = (
    "Technology & AI",
    "Business & Startups",
    "Design",
    "Science",
    "History",
    "Finance",
    "Culture",
    "Politics",
    "Health & Fitness",
    "Comedy",
    "True Crime",
    "Sport",
    "Personal Development",
    "Food & Cooking",
    "Music",
    "Film & TV",
    "Books & Writing",
    "Philosophy",
    "Climate & Energy",
    "Travel",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Each interest costs one expansion slot and TERMS_PER_INTEREST searches. Past
# six the universe stops being about anything in particular.
MAX_INTERESTS = 6
MIN_CHIPS_WITHOUT_TEXT = 3


# --- the build job -----------------------------------------------------------


@dataclass
class Job:
    """One in-flight universe build. Lives in memory; a restart forgets it."""

    email: str
    user_id: int
    interests: list[str]
    state: str = "building"  # building | done | failed
    started_at: float = field(default_factory=time.monotonic)
    shows: int = 0
    preview: list[str] = field(default_factory=list)
    error: str = ""


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)


def run_build(job_id: str) -> None:
    """Search, rank and persist the candidate universe. Runs off-request.

    Failures are recorded on the job rather than swallowed. A user row with no
    candidate shows is the one outcome the product cannot recover from on its
    own, so it has to be visible on the page.
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return

    try:
        with db.session() as conn:
            kept = universe.build(conn, job.user_id, job.interests)
        _set(
            job_id,
            state="done",
            shows=len(kept),
            preview=[hit.title for hit in kept[:12]],
        )
    except Exception as exc:  # noqa: BLE001 — reported to the page verbatim
        traceback.print_exc()
        _set(job_id, state="failed", error=f"{type(exc).__name__}: {exc}")


# --- form handling -----------------------------------------------------------


def parse_interests(specifics: str, chips: list[str]) -> list[str]:
    """One interest per non-empty line of the text box.

    Lines still holding nothing but a chip seed ("Design —") are dropped: the
    user opened the line and did not fill it in, and a bare category label is
    exactly the abstraction the text box exists to avoid.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for raw in specifics.splitlines():
        text = re.sub(r"\s+", " ", raw.strip().lstrip("-•*").strip())
        text = re.sub(r"[\s\-–—:,]+$", "", text)
        if len(text) < 3 or text in chips:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(text)

    return (lines or chips)[:MAX_INTERESTS]


def validate(email: str, interests: list[str], chips: list[str]) -> str:
    if not EMAIL_RE.match(email):
        return "That email address does not look right."
    if not interests:
        return "Tell us what you want to hear about, in your own words."
    if not any(text not in chips for text in interests) and len(chips) < MIN_CHIPS_WITHOUT_TEXT:
        return f"Pick at least {MIN_CHIPS_WITHOUT_TEXT} areas, or write a line of your own."
    return ""


# --- routes ------------------------------------------------------------------


@app.get("/")
def onboard():
    return render_template(
        "onboard.html",
        chips=CHIPS,
        missing_keys=config.missing_keys(2),
        form={},
        error=None,
    )


@app.post("/subscribe")
def subscribe():
    email = request.form.get("email", "").strip()
    specifics = request.form.get("specifics", "")
    chips = [c for c in request.form.getlist("chip") if c in CHIPS]

    interests = parse_interests(specifics, chips)
    error = validate(email, interests, chips)
    if error:
        return (
            render_template(
                "onboard.html",
                chips=CHIPS,
                missing_keys=config.missing_keys(2),
                form={"email": email, "specifics": specifics, "chips": chips},
                error=error,
            ),
            400,
        )

    # Write the user and their interests synchronously: it takes milliseconds,
    # and it means a build that fails later still leaves the interests on
    # record to retry against.
    with db.session() as conn:
        user_id = universe.ensure_user(conn, email)
        universe.save_interests(
            conn, user_id, [universe.Interest(text=text, terms=[]) for text in interests]
        )

    job_id = secrets.token_urlsafe(9)
    with JOBS_LOCK:
        JOBS[job_id] = Job(email=email, user_id=user_id, interests=interests)
    threading.Thread(target=run_build, args=(job_id,), daemon=True).start()

    return redirect(url_for("done", job_id=job_id))


@app.get("/done/<job_id>")
def done(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return render_template("done.html", job_id=None, job=None), 404
    return render_template("done.html", job_id=job_id, job=job)


@app.get("/status/<job_id>")
def status(job_id: str):
    """Polled by the done page. The build has no other way to report itself."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"state": "unknown"}), 404
    return jsonify(
        {
            "state": job.state,
            "shows": job.shows,
            "preview": job.preview,
            "error": job.error,
            "elapsed": round(time.monotonic() - job.started_at, 1),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=5001)  # 5000 is AirPlay on macOS
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    db.init_db()
    missing = config.missing_keys(2)
    if missing:
        print(f"warn  {', '.join(missing)} missing from .env — the build will fail")
    print(f"onboarding at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
