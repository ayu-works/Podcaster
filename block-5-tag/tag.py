"""Tag each new episode once for the shared pool (ARCHITECTURE section 6)."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from groq import APIStatusError, Groq, RateLimitError

from _shared import config, db

SYSTEM_PROMPT = f"""You tag podcast episodes for a recommendation service. You are a filter,
not a feed. Your job is to protect listeners' time.

For each episode return:
- topics: 0-3 slugs from the allowed list. Only slugs the episode is
  genuinely ABOUT. An empty array is correct and common - an episode that
  fits nothing is dropped, which is the desired outcome.
- score: 0-100, how much this is worth a listener's time. 70 means "worth
  it". Be harsh; most episodes are not worth most people's time. Prefer
  episodes whose description names a concrete claim, guest, case study or
  number. Vague descriptions score low even when on-topic.
- why: ONE sentence that MUST reference something specific from that
  episode's description - a named guest, a claim, a case study, a number.
  Generic praise ("a great listen", "perfect for anyone interested in X")
  is a failed output, not a weak one.

Allowed topic slugs:
{", ".join(config.TOPIC_SLUGS)}

Output ONLY valid JSON:
{{"episodes":[{{"id":<int>,"topics":["<slug>"],"score":<int>,"why":"<one sentence>"}}]}}"""

RETRY_NOTE = (
    "Your previous response could not be parsed as the required JSON: {error}\n"
    "Return every episode again and output only the JSON object."
)

_GENERIC_PHRASES = (
    "great listen", "must-listen", "must listen", "highly relevant",
    "worth a listen", "right up your", "perfect for anyone", "if you are interested",
    "anyone interested in", "a great episode", "sure to appeal", "will appreciate",
)
_WORD_RE = re.compile(r"[a-z][a-z'-]{4,}")


class TagError(RuntimeError):
    """The tagging service failed in a way that must stop the pipeline."""


class AttemptsExhausted(RuntimeError):
    """An episode reached its bounded retry cap."""


class BudgetExhausted(RuntimeError):
    """The daily token budget cannot safely fit another call."""


class DeadlineReached(RuntimeError):
    """The remaining wall clock cannot safely fit another call."""


@dataclass
class ParsedTag:
    index: int
    topics: list[str]
    score: int
    why: str


@dataclass
class TagResult:
    selected: int = 0
    attempted: int = 0
    tagged: int = 0
    generic: int = 0
    invalid: int = 0
    parse_failed: int = 0
    abandoned: int = 0
    untagged_left: int = 0
    tokens_used: int = 0
    budget_exhausted: bool = False
    deadline_reached: bool = False
    rows: list[tuple[int, int, list[str], str]] = field(default_factory=list)


@dataclass
class CallState:
    tokens_used: int = 0
    last_call_at: float | None = None
    last_call_tokens: int = 0
    attempts: dict[int, int] = field(default_factory=dict)
    # Monotonic instant after which no further call may start, and how long the
    # slowest batch so far actually took. Measured rather than assumed: pacing
    # and model latency both vary by an order of magnitude between runs.
    #
    # The slowest batch, not the most recent one, because the estimate only has
    # to be right in one direction. One quick batch would otherwise shrink the
    # estimate enough to admit a slow batch that runs past the step timeout —
    # the exact death this deadline exists to prevent. Overestimating merely
    # ends the stage an batch early, which costs a few episodes; underestimating
    # costs the whole digest.
    deadline: float | None = None
    slowest_batch_seconds: float = 0.0


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "?"
    return f"{round(seconds / 60)}m"


def looks_generic(reason: str, description: str) -> bool:
    """True when a reason could have been written without reading the episode."""
    low = reason.casefold()
    if any(phrase in low for phrase in _GENERIC_PHRASES):
        return True
    desc_words = set(_WORD_RE.findall((description or "").casefold()))
    return not desc_words & set(_WORD_RE.findall(low))


def candidate_line(index: int, row: sqlite3.Row) -> str:
    return (
        f"[{index}] show: {row['show_name']} | title: {row['title']} | "
        f"{format_duration(row['duration_sec'])} | "
        f"desc: {(row['description'] or '')[: config.DESC_TRUNCATE]}"
    )


def build_prompt(rows: list[sqlite3.Row]) -> str:
    return "\n".join(candidate_line(index, row) for index, row in enumerate(rows, 1))


def log_call(batch_id: int, label: str, prompt: str, raw: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with config.TAG_LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(
            f"\n{'=' * 78}\n{stamp}  batch={batch_id}  model={config.GROQ_MODEL}  {label}\n"
            f"{'=' * 78}\n--- SYSTEM ---\n{SYSTEM_PROMPT}\n"
            f"--- USER ---\n{prompt}\n--- RESPONSE ---\n{raw}\n"
        )


def parse_tags(raw: str, candidate_count: int) -> tuple[dict[int, ParsedTag], int]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("episodes"), list):
        raise ValueError("response must contain an 'episodes' list")

    parsed: dict[int, ParsedTag] = {}
    invalid = 0
    for entry in data["episodes"]:
        try:
            index = int(entry["id"])
            score = int(entry["score"])
            why = str(entry["why"]).strip()
            topics = entry["topics"]
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if (
            not 1 <= index <= candidate_count
            or index in parsed
            or not 0 <= score <= 100
            or not why
            or not isinstance(topics, list)
        ):
            invalid += 1
            continue
        valid_topics = [
            topic
            for topic in dict.fromkeys(str(topic) for topic in topics)
            if topic in config.TOPIC_SLUGS
        ][: config.TAG_MAX_TOPICS]
        parsed[index] = ParsedTag(index, valid_topics, score, why)
    return parsed, invalid


def load_queue(
    conn,
    limit: int | None = None,
    episode_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT * FROM episode WHERE tagged_at IS NULL AND tag_attempts < ? "
    )
    params: list[int] = [config.TAG_MAX_ATTEMPTS]
    if episode_ids is not None:
        if not episode_ids:
            return []
        placeholders = ",".join("?" for _ in episode_ids)
        sql += f" AND id IN ({placeholders})"
        params.extend(episode_ids)
    sql += " ORDER BY published_at DESC, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def estimate_call_tokens(prompt: str) -> int:
    input_tokens = (len(SYSTEM_PROMPT) + len(prompt)) // 4 + 1
    return input_tokens + config.TAG_COMPLETION_TOKENS


def _pace(state: CallState) -> None:
    if state.last_call_at is None or state.last_call_tokens <= 0:
        return
    interval = state.last_call_tokens / config.GROQ_TPM * 60
    now = time.monotonic()
    remaining = interval - (now - state.last_call_at)
    if remaining <= 0:
        return
    # Sleeping past the deadline would spend the whole margin waiting and then
    # start a call that cannot finish. Stop instead, so the caller keeps what
    # is already committed.
    if state.deadline is not None and now + remaining > state.deadline:
        raise DeadlineReached
    time.sleep(remaining)


def _retry_after(exc: APIStatusError) -> float:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


def _record_attempt(
    conn,
    rows: list[sqlite3.Row],
    state: CallState,
    dry_run: bool,
) -> None:
    for row in rows:
        state.attempts[row["id"]] = state.attempts.get(
            row["id"], row["tag_attempts"]
        ) + 1
    if dry_run:
        return
    episode_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in episode_ids)
    conn.execute(
        "UPDATE episode SET tag_attempts = tag_attempts + 1, tag_error = NULL "
        f"WHERE id IN ({placeholders})",
        episode_ids,
    )
    # Never hold a remote transaction open while waiting for Groq. Besides
    # blocking concurrent writers, Turso can retire the idle HTTP stream.
    conn.commit()


def _set_batch_error(conn, rows: list[sqlite3.Row], message: str) -> None:
    """Set one shared error without one Turso request per episode."""
    if not rows:
        return
    episode_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in episode_ids)
    conn.execute(
        f"UPDATE episode SET tag_error=? WHERE id IN ({placeholders})",
        (message, *episode_ids),
    )


def _call(
    conn,
    rows: list[sqlite3.Row],
    messages: list[dict[str, str]],
    prompt: str,
    batch_id: int,
    client: Groq,
    state: CallState,
    dry_run: bool,
    budget: int,
    request_progress=None,
) -> str:
    for retry in range(config.TAG_MAX_ATTEMPTS):
        if any(
            state.attempts.get(row["id"], row["tag_attempts"])
            >= config.TAG_MAX_ATTEMPTS
            for row in rows
        ):
            raise AttemptsExhausted
        if state.tokens_used + estimate_call_tokens(prompt) > budget:
            raise BudgetExhausted
        _pace(state)
        db.ensure_connection(conn)
        _record_attempt(conn, rows, state, dry_run)
        try:
            state.last_call_at = time.monotonic()
            if request_progress is not None:
                request_progress(batch_id, retry + 1)
            response = client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=messages,
                temperature=0,
                max_completion_tokens=config.TAG_COMPLETION_TOKENS,
                reasoning_effort=config.TAG_REASONING_EFFORT,
                response_format={"type": "json_object"},
            )
        except RateLimitError as exc:
            if retry == config.TAG_MAX_ATTEMPTS - 1:
                raise TagError("Groq rate limit persisted through all attempts") from exc
            time.sleep(max(_retry_after(exc), 2**retry))
            continue
        except Exception as exc:
            raise TagError(f"Groq tagging call failed: {type(exc).__name__}: {exc}") from exc

        raw = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        used = int(getattr(usage, "total_tokens", 0) or estimate_call_tokens(prompt))
        state.last_call_tokens = used
        state.tokens_used += used
        # The model call itself can outlive Turso's idle stream. Refresh with
        # a retry-safe read before the next database operation.
        db.ensure_connection(conn)
        log_call(batch_id, "retry" if len(messages) > 2 else "tag", prompt, raw)
        return raw
    raise TagError("unreachable call retry state")


def _write_batch(
    conn,
    rows: list[sqlite3.Row],
    parsed: dict[int, ParsedTag],
    result: TagResult,
    dry_run: bool,
) -> None:
    valid: list[tuple[sqlite3.Row, ParsedTag]] = []
    missing: list[sqlite3.Row] = []
    generic: list[sqlite3.Row] = []
    for index, row in enumerate(rows, 1):
        tag = parsed.get(index)
        if tag is None:
            result.invalid += 1
            missing.append(row)
            continue
        if looks_generic(tag.why, row["description"]):
            result.generic += 1
            generic.append(row)
            continue

        result.tagged += 1
        result.rows.append((row["id"], tag.score, tag.topics, tag.why))
        valid.append((row, tag))

    if dry_run:
        return
    _set_batch_error(conn, missing, "missing or invalid response entry")
    _set_batch_error(conn, generic, "generic or ungrounded why")
    if not valid:
        return

    score_cases = " ".join("WHEN ? THEN ?" for _ in valid)
    why_cases = " ".join("WHEN ? THEN ?" for _ in valid)
    valid_ids = [row["id"] for row, _ in valid]
    id_placeholders = ",".join("?" for _ in valid_ids)
    parameters = [
        value
        for row, tag in valid
        for value in (row["id"], tag.score)
    ]
    parameters.extend(
        value
        for row, tag in valid
        for value in (row["id"], tag.why)
    )
    parameters.extend(valid_ids)
    conn.execute(
        "UPDATE episode SET "
        f"score=CASE id {score_cases} END, "
        f"why=CASE id {why_cases} END, "
        "tagged_at=datetime('now'), tag_error=NULL "
        f"WHERE id IN ({id_placeholders})",
        parameters,
    )
    # Rows enter this function only while tagged_at is NULL, so they cannot
    # already have model-derived topics. Insert all topics in one statement.
    db.execute_values(
        conn,
        "INSERT INTO episode_topic (episode_id, topic) VALUES {values}",
        [
            (row["id"], topic)
            for row, tag in valid
            for topic in tag.topics
        ],
    )


def tag_all(
    conn,
    limit: int | None = None,
    dry_run: bool = False,
    client: Groq | None = None,
    daily_budget: int | None = None,
    episode_ids: list[int] | None = None,
    progress=None,
    request_progress=None,
    request_timeout_seconds: float | None = None,
    deadline_seconds: float | None = None,
) -> TagResult:
    if not config.GROQ_API_KEY and client is None:
        raise TagError("GROQ_API_KEY missing from .env; tagging cannot run")
    started_at = time.monotonic()
    rows = load_queue(conn, limit, episode_ids=episode_ids)
    result = TagResult(selected=len(rows))
    if not rows:
        return result

    if request_timeout_seconds is not None and request_timeout_seconds <= 0:
        raise ValueError("Groq request timeout must be positive")
    groq = client or Groq(
        api_key=config.GROQ_API_KEY,
        **(
            {"timeout": request_timeout_seconds, "max_retries": 0}
            if request_timeout_seconds is not None
            else {}
        ),
    )
    budget = config.GROQ_TPD if daily_budget is None else daily_budget
    if deadline_seconds is None:
        deadline_seconds = config.TAG_DEADLINE_SECONDS
    state = CallState(
        deadline=started_at + deadline_seconds if deadline_seconds > 0 else None
    )

    for start in range(0, len(rows), config.TAG_BATCH_SIZE):
        batch = rows[start : start + config.TAG_BATCH_SIZE]
        # The first batch always runs. A pessimistic estimate that returns zero
        # tagged episodes is worse than one call that overshoots, because the
        # margin exists precisely to absorb the overshoot.
        if (
            start
            and state.deadline is not None
            and time.monotonic() + state.slowest_batch_seconds > state.deadline
        ):
            result.deadline_reached = True
            break
        batch_started_at = time.monotonic()
        prompt = build_prompt(batch)
        if state.tokens_used + estimate_call_tokens(prompt) > budget:
            result.budget_exhausted = True
            break

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        parsed: dict[int, ParsedTag] | None = None
        parse_error: Exception | None = None
        attempts_before = sum(state.attempts.values())
        stop_for_budget = False
        stop_for_deadline = False
        for parse_attempt in range(2):
            try:
                raw = _call(
                    conn, batch, messages, prompt, start // config.TAG_BATCH_SIZE + 1,
                    groq, state, dry_run, budget, request_progress,
                )
                parsed, invalid = parse_tags(raw, len(batch))
                result.invalid += invalid
                break
            except TagError as exc:
                if not dry_run:
                    db.ensure_connection(conn)
                    _set_batch_error(conn, batch, str(exc))
                    conn.commit()
                raise
            except BudgetExhausted:
                result.budget_exhausted = True
                stop_for_budget = True
                break
            except DeadlineReached:
                result.deadline_reached = True
                stop_for_deadline = True
                break
            except AttemptsExhausted:
                break
            except (json.JSONDecodeError, ValueError) as exc:
                parse_error = exc
                if parse_attempt == 0:
                    messages += [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": RETRY_NOTE.format(error=exc)},
                    ]
        if sum(state.attempts.values()) > attempts_before:
            result.attempted += len(batch)
        if parsed is None:
            if parse_error is not None:
                result.parse_failed += len(batch)
            # A batch the clock stopped before it began has nothing wrong with
            # it; stamping an error would make the next run's queue look like a
            # quality problem rather than a short morning.
            if not dry_run and not (stop_for_deadline and parse_error is None):
                _set_batch_error(
                    conn,
                    batch,
                    f"unparseable response: {parse_error}"
                    if parse_error is not None
                    else "tag attempts exhausted",
                )
        else:
            _write_batch(conn, batch, parsed, result, dry_run)
        if not dry_run:
            conn.commit()
        if progress is not None:
            progress(min(start + len(batch), len(rows)), len(rows), result)
        if stop_for_budget or stop_for_deadline:
            break
        state.slowest_batch_seconds = max(
            state.slowest_batch_seconds, time.monotonic() - batch_started_at
        )

    result.tokens_used = state.tokens_used
    if not dry_run and episode_ids is not None:
        tagged_ids = {row[0] for row in result.rows}
        result.abandoned = sum(
            row["id"] not in tagged_ids
            and state.attempts.get(row["id"], row["tag_attempts"])
            >= config.TAG_MAX_ATTEMPTS
            for row in rows
        )
        result.untagged_left = result.selected - result.tagged - result.abandoned
    elif not dry_run:
        result.untagged_left = conn.execute(
            "SELECT COUNT(*) FROM episode WHERE tagged_at IS NULL "
            "AND tag_attempts < ?",
            (config.TAG_MAX_ATTEMPTS,),
        ).fetchone()[0]
        result.abandoned = conn.execute(
            "SELECT COUNT(*) FROM episode WHERE tagged_at IS NULL "
            "AND tag_attempts >= ?",
            (config.TAG_MAX_ATTEMPTS,),
        ).fetchone()[0]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with db.session() as conn:
        result = tag_all(conn, limit=args.limit, dry_run=args.dry_run)

    print(
        f"selected {result.selected}, attempted {result.attempted}, tagged {result.tagged}, "
        f"generic {result.generic}, invalid {result.invalid}, "
        f"tokens {result.tokens_used}, untagged_left {result.untagged_left}, "
        f"abandoned {result.abandoned}"
    )
    if result.budget_exhausted:
        print("daily token budget reached; remainder left untouched for the next run")
    if result.deadline_reached:
        print("tagging deadline reached; remainder left untouched for the next run")
    for episode_id, score, topics, why in result.rows[:20]:
        print(f"{episode_id:>6}  {score:>3}  {','.join(topics) or '-'}  {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
