"""Pick the best episodes for one listener. One Groq call (ARCHITECTURE section 6, stage 3).

**This is the product. Everything else is plumbing.**

The prompt is the whole of it, and the rules are in priority order for a reason:
topical fit first, specificity second, an honest reason third, harshness fourth.
The hypothesis the product rests on is that a model reading episode metadata
against stated interests can pick well *and can stay quiet when it cannot* — so
returning nothing has to be a first-class outcome, not an error path.

Two rules are enforced in code rather than trusted to the prompt: the relevance
bar (it is a config number, and calibrating it should take seconds) and never
two episodes from one show in a single send (PRD rule 6 — a hard constraint,
and code is cheaper than prompt tokens).

**The pool is cut before the call, and that is a real limitation.** A run
produces ~180 candidates, or ~25k tokens; Groq's free tier allows 8k per
minute. So `select_pool` keeps what fits and the rest are never seen by the
ranker at all. The cut is structural, not editorial — newest first, capped per
show — because any topical pre-filter would be a keyword heuristic quietly
overruling the judgement this block exists to make. Raising `GROQ_TPM` after a
tier upgrade widens the pool on its own, with no code change.

Every prompt and raw response is appended to `logs/rank.log`. When picks are bad
this is how you find out why, and you will read it constantly in week one.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from groq import Groq

from _shared import config, db
import fetch as fetch_mod

SYSTEM_PROMPT = """You rank podcast episodes for one specific listener. You are their
filter, not their feed. Your job is to protect their time.

Rules, in priority order:
1. Topical fit beats popularity. A small show that nails their exact
   interest beats a big show that is merely adjacent.
2. Prefer episodes whose description names a concrete claim, guest, or
   case study. Vague descriptions score low even when on-topic.
3. Your reason MUST reference something specific from that episode's
   description — a named guest, a claim, a case study, a number. Generic
   praise ("a great listen", "highly relevant", "perfect for anyone
   interested in X") is a failed output.
4. Score 0-100. 70 means "worth their time". Be harsh. Most episodes
   are not worth most people's time.

Where two candidates are otherwise equal, prefer the shorter one.

Returning fewer picks than asked for is CORRECT when the candidates
do not deserve it. An empty array is a valid and often correct answer.
Never pad.

Output ONLY valid JSON:
{"picks":[{"id":<int>,"score":<int>,"reason":"<one sentence>"}]}"""

RETRY_NOTE = (
    "Your previous response could not be parsed as the required JSON: {error}\n"
    "Return only the JSON object, with no prose, code fences, or commentary."
)

# GROQ_MODEL is a reasoning model: `max_completion_tokens` covers its thinking
# as well as its answer, and a starved budget returns nothing at all — a 400
# with `json_validate_failed` and an empty generation, not a short reply.
COMPLETION_TOKENS = 3000
REASONING_EFFORT = "medium"

# Phrases that are generic praise no matter what else the sentence contains.
_GENERIC_PHRASES = (
    "great listen", "must-listen", "must listen", "highly relevant",
    "worth a listen", "right up your", "perfect for anyone", "if you are interested",
    "anyone interested in", "a great episode", "sure to appeal", "will appreciate",
)
_WORD_RE = re.compile(r"[a-z][a-z'-]{4,}")

# Characters per token. Deliberately low — under-estimating wastes a little of
# the pool, over-estimating costs a 413 at 7am.
_CHARS_PER_TOKEN = 3.2


class RankError(RuntimeError):
    """The ranker could not run at all. A bad *result* is not this."""


@dataclass
class Pick:
    episode: sqlite3.Row
    score: int
    reason: str

    @property
    def generic(self) -> bool:
        return looks_generic(self.reason, self.episode["description"])


@dataclass
class RankResult:
    picks: list[Pick] = field(default_factory=list)
    candidates: int = 0  # what fetch handed over
    ranked: int = 0  # what actually reached the model
    returned: int = 0  # picks the model made, before our filtering
    below_bar: int = 0
    dropped_same_show: int = 0
    invalid: int = 0
    retried: bool = False
    failed: bool = False
    elapsed: float = 0.0
    error: str = ""

    @property
    def unseen(self) -> int:
        """Candidates the rate limit cost us. Watch this number."""
        return self.candidates - self.ranked

    @property
    def cleared_bar(self) -> int:
        """ARCHITECTURE section 10's middle number: good picks before the cap."""
        return self.returned - self.below_bar - self.invalid

    @property
    def trustworthy(self) -> bool:
        """Whether an empty result means "nothing was good enough".

        Without this, a broken run and a genuinely quiet day are the same
        observation, and the product's whole promise rests on silence being
        meaningful. Never record a quiet digest when this is False.
        """
        return not self.failed


# --- the prompt --------------------------------------------------------------


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "?"
    return f"{round(seconds / 60)}m"


def candidate_line(index: int, row: sqlite3.Row) -> str:
    return (
        f"[{index}] show: {row['show_name']} | title: {row['title']} | "
        f"{format_duration(row['duration_sec'])} | "
        f"desc: {(row['description'] or '')[: config.DESC_TRUNCATE]}"
    )


def build_prompt(interests: list[str], candidates: list[sqlite3.Row]) -> str:
    """The user half of the call.

    Candidates are numbered from 1 rather than carrying database ids: small
    integers are cheaper and the model makes fewer transcription mistakes with
    them. `rank` maps them back.
    """
    lines = [candidate_line(i, row) for i, row in enumerate(candidates, 1)]
    return (
        "Interests:\n" + "\n".join(interests)
        + "\n\nCandidates:\n" + "\n".join(lines)
        + f"\n\nReturn up to {config.PICKS_PER_EMAIL} picks scoring "
        f"{config.RELEVANCE_BAR} or above."
    )


def estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + 1


# --- cutting the pool --------------------------------------------------------


def select_pool(interests: list[str], candidates: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Choose the candidates that fit in one call.

    Shows are interleaved rather than taken strictly newest-first: a daily news
    podcast publishes five episodes in a five-day window and would otherwise
    spend five of roughly thirty slots, silencing four other shows. Within that,
    newest first.

    This is deliberately a *structural* cut. Ranking the pool by keyword overlap
    first would be cheaper and would look smarter, and it would also let a
    string match overrule the judgement this entire block exists to make.
    """
    limit = min(config.RANK_PROMPT_TOKENS, config.GROQ_TPM - COMPLETION_TOKENS)
    budget = limit - estimate_tokens(SYSTEM_PROMPT + "\n".join(interests)) - 120

    by_show: dict[str, list[sqlite3.Row]] = {}
    for row in candidates:  # already newest-first from fetch
        by_show.setdefault((row["show_name"] or "").casefold(), []).append(row)

    # Round-robin across shows, newest episode of each first.
    ordered: list[sqlite3.Row] = []
    for depth in range(config.RANK_MAX_PER_SHOW):
        for episodes in by_show.values():
            if depth < len(episodes):
                ordered.append(episodes[depth])

    pool: list[sqlite3.Row] = []
    used = 0
    for row in ordered:
        cost = estimate_tokens(candidate_line(99, row))
        if used + cost > budget:
            break
        pool.append(row)
        used += cost
    return pool


# --- parsing -----------------------------------------------------------------


def parse_picks(raw: str, candidate_count: int) -> tuple[list[tuple[int, int, str]], int]:
    """Pull (index, score, reason) out of the response. Returns (picks, invalid).

    Malformed *entries* are dropped and counted; malformed *JSON* raises so the
    caller can retry once. One bad pick should not cost the whole run.
    """
    text = raw.strip()
    if text.startswith("```"):  # some models fence despite JSON mode
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    entries = data.get("picks")
    if entries is None:
        raise ValueError("response has no 'picks' key")
    if not isinstance(entries, list):
        raise ValueError("'picks' is not a list")

    picks: list[tuple[int, int, str]] = []
    invalid = 0
    for entry in entries:
        try:
            index = int(entry["id"])
            score = int(entry["score"])
            reason = str(entry["reason"]).strip()
        except (TypeError, ValueError, KeyError):
            invalid += 1
            continue
        if not 1 <= index <= candidate_count or not reason or not 0 <= score <= 100:
            invalid += 1
            continue
        picks.append((index, score, reason))
    return picks, invalid


def looks_generic(reason: str, description: str) -> bool:
    """True when a reason could have been written without reading the episode.

    Rule 3 is the one the plan calls a bug rather than a weak output, so it gets
    a check instead of a hope. Two signals: a stock phrase, or no substantial
    word shared with the description — a reason grounded in the episode almost
    always reuses one of its content words.
    """
    low = reason.casefold()
    if any(phrase in low for phrase in _GENERIC_PHRASES):
        return True
    desc_words = set(_WORD_RE.findall((description or "").casefold()))
    return not desc_words & set(_WORD_RE.findall(low))


# --- the call ----------------------------------------------------------------


def log_call(user_id: int, label: str, prompt: str, raw: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with config.RANK_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(
            f"\n{'=' * 78}\n{stamp}  user={user_id}  model={config.GROQ_MODEL}  {label}\n"
            f"{'=' * 78}\n--- SYSTEM ---\n{SYSTEM_PROMPT}\n"
            f"--- USER ---\n{prompt}\n--- RESPONSE ---\n{raw}\n"
        )


def rank(
    conn,
    user_id: int,
    candidates: list[sqlite3.Row],
    client: Groq | None = None,
) -> RankResult:
    """One call, strict JSON, one retry, then empty. A quiet day beats a stack trace."""
    interests = [
        row["text"]
        for row in conn.execute(
            "SELECT text FROM interest WHERE user_id = ? ORDER BY id", (user_id,)
        )
    ]
    if not interests:
        raise RankError(f"User {user_id} has no interests — nothing to rank against.")
    if not config.GROQ_API_KEY and client is None:
        raise RankError("GROQ_API_KEY missing from .env; Block 5 cannot run without it.")

    started = time.time()
    result = RankResult(candidates=len(candidates))
    pool = select_pool(interests, candidates)
    result.ranked = len(pool)
    if not pool:
        return result

    groq = client or Groq(api_key=config.GROQ_API_KEY)
    prompt = build_prompt(interests, pool)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    parsed: list[tuple[int, int, str]] = []
    for attempt in range(2):
        try:
            response = groq.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=messages,
                temperature=0,
                max_completion_tokens=COMPLETION_TOKENS,
                reasoning_effort=REASONING_EFFORT,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 — the API itself failed
            result.failed = True
            result.error = f"{type(exc).__name__}: {exc}"
            result.elapsed = time.time() - started
            return result

        raw = response.choices[0].message.content or ""
        log_call(user_id, "retry" if attempt else "rank", prompt, raw)

        try:
            parsed, result.invalid = parse_picks(raw, len(pool))
            break
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 1:
                # Empty, not an exception. A quiet day is a valid outcome; a
                # crashed cron job at 7am is not. `failed` keeps the two apart.
                result.failed = True
                result.error = f"unparseable response: {exc}"
                result.elapsed = time.time() - started
                return result
            result.retried = True
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": RETRY_NOTE.format(error=exc)},
            ]

    result.returned = len(parsed) + result.invalid

    # The bar is applied here, not left to the prompt. It is the number most
    # likely to move in week one, and it should move by editing config.py.
    scored = [p for p in parsed if p[1] >= config.RELEVANCE_BAR]
    result.below_bar = len(parsed) - len(scored)
    scored.sort(key=lambda p: -p[1])

    # PRD rule 6: never two episodes from the same show in one send.
    seen_shows: set[str] = set()
    for index, score, reason in scored:
        if len(result.picks) >= config.PICKS_PER_EMAIL:
            break
        episode = pool[index - 1]
        show = (episode["show_name"] or "").casefold()
        if show in seen_shows:
            result.dropped_same_show += 1
            continue
        seen_shows.add(show)
        result.picks.append(Pick(episode=episode, score=score, reason=reason))

    result.elapsed = time.time() - started
    return result


def rank_for_user(conn, user_id: int, client: Groq | None = None):
    """Fetch then rank. The pairing Block 7 will call."""
    fetched = fetch_mod.fetch_for_user(conn, user_id)
    return rank(conn, user_id, fetched.candidates, client=client), fetched


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--runs", type=int, default=1, help="repeat the ranking; the check calls for 3"
    )
    parser.add_argument("--bar", type=int, help="override RELEVANCE_BAR for this run")
    args = parser.parse_args()

    if args.bar is not None:
        config.RELEVANCE_BAR = args.bar

    with db.session() as conn:
        user = conn.execute("SELECT id FROM user WHERE email = ?", (args.email,)).fetchone()
        if user is None:
            print(f"No user {args.email}. Subscribe first (Block 3).", file=sys.stderr)
            return 1

        fetched = fetch_mod.fetch_for_user(conn, user["id"])
        print(
            f"{fetched.raw} raw -> {fetched.after_filter} after filter"
            f"  (bar {config.RELEVANCE_BAR}, up to {config.PICKS_PER_EMAIL} picks)"
        )

        generic_total = 0
        for run in range(1, args.runs + 1):
            if run > 1:
                # One call is ~5k tokens and the free tier allows 8k a minute.
                print("\n  (waiting out the rate limit before the next run)", flush=True)
                time.sleep(62)

            result = rank(conn, user["id"], fetched.candidates)
            line = (
                f"\n--- run {run}/{args.runs}  ranked {result.ranked} of "
                f"{result.candidates}, returned {result.returned}, "
                f"cleared bar {result.cleared_bar}, sending {len(result.picks)}"
                f"  [{result.elapsed:.0f}s]"
            )
            for label, count in (
                ("below bar", result.below_bar),
                ("invalid", result.invalid),
                ("same show", result.dropped_same_show),
            ):
                if count:
                    line += f", {label} {count}"
            print(line + (" [retried]" if result.retried else ""))
            if result.error:
                print(f"  error: {result.error}")
            if result.unseen:
                print(
                    f"  {result.unseen} candidates never reached the model "
                    f"(rate limit). Raise GROQ_TPM if your tier allows."
                )

            if not result.picks:
                print(
                    "  (quiet — nothing cleared the bar. This is a valid answer.)"
                    if result.trustworthy
                    else "  NOT a quiet day — the call failed. "
                    "Do not record this as a quiet digest."
                )
            for pick in result.picks:
                episode = pick.episode
                flag = "  <-- GENERIC, see rule 3" if pick.generic else ""
                generic_total += pick.generic
                print(f"\n  {pick.score:>3}  {episode['show_name'][:56]}")
                print(f"       {episode['title'][:66]}")
                print(
                    f"       {format_duration(episode['duration_sec'])}"
                    f"  {(episode['published_at'] or '')[:10]}"
                )
                print(f"       “{pick.reason}”{flag}")

    # THE CHECK. Read the reasons out loud. A generic reason is a bug, not a
    # weak output — tighten rule 3 and rerun before moving on.
    print(f"\nfull prompts and responses: {config.RANK_LOG_PATH}")
    if generic_total:
        print(
            f"\n{generic_total} reason(s) did not reference the episode. That is rule 3 "
            "failing, and it is a bug — tighten the system prompt and rerun."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
