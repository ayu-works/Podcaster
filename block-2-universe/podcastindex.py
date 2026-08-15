"""Podcast Index API client.

The auth trips everyone up. Every request carries three headers, and the
`Authorization` value is sha1(key + secret + unix_seconds) as hex. The
timestamp inside that hash must be the *same* value sent as `X-Auth-Date`,
or the signature will not validate.

Failures raise. A run that silently returns zero candidates looks identical
to a quiet week from the outside, and that is the failure mode this whole
product is worst at noticing (ARCHITECTURE section 10).
"""

import hashlib
import time

import httpx

from _shared import config

BASE_URL = "https://api.podcastindex.org/api/1.0"
USER_AGENT = "Podcaster/0.1"
TIMEOUT = 20.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.5  # seconds; 1.5, 3, 6


class PodcastIndexError(RuntimeError):
    """A call failed after exhausting retries, or returned an API-level error."""


def _auth_headers() -> dict[str, str]:
    stamp = str(int(time.time()))
    digest = hashlib.sha1(
        (config.PODCASTINDEX_KEY + config.PODCASTINDEX_SECRET + stamp).encode("utf-8")
    ).hexdigest()
    return {
        "X-Auth-Key": config.PODCASTINDEX_KEY,
        "X-Auth-Date": stamp,
        "Authorization": digest,
        "User-Agent": USER_AGENT,
    }


def _get(path: str, params: dict, client: httpx.Client | None = None) -> dict:
    """GET with retry and backoff on transport errors, 429s and 5xxs.

    4xx other than 429 are not retried — a bad key or a malformed query will
    fail the same way three times, and the wait only hides the real error.
    """
    if not config.PODCASTINDEX_KEY or not config.PODCASTINDEX_SECRET:
        raise PodcastIndexError(
            "PODCASTINDEX_KEY / PODCASTINDEX_SECRET missing from .env. "
            "Both are required — the signature is built from the pair."
        )

    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    last_error: Exception | None = None
    try:
        for attempt in range(MAX_RETRIES):
            try:
                # Headers are rebuilt per attempt: the signature embeds a
                # timestamp, and a stale one is rejected.
                response = client.get(
                    BASE_URL + path, params=params, headers=_auth_headers()
                )
                if response.status_code == 401:
                    raise PodcastIndexError(
                        "Podcast Index rejected the signature (401). Check that "
                        "PODCASTINDEX_SECRET is the full secret from the signup "
                        "email, and that the system clock is accurate."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = PodcastIndexError(
                        f"{response.status_code} from {path}"
                    )
                else:
                    response.raise_for_status()
                    return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc

            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2**attempt))

        raise PodcastIndexError(f"{path} failed after {MAX_RETRIES} attempts") from last_error
    finally:
        if owned:
            client.close()


def search_shows(
    term: str, max_results: int | None = None, client: httpx.Client | None = None
) -> list[dict]:
    """Search *show* names and descriptions for `term`.

    Note this searches shows, not episodes — the limitation that shapes the
    whole design (ARCHITECTURE section 2). Results are ordered by the API's
    own relevance, which universe.py relies on when merging term results.
    """
    data = _get(
        "/search/byterm",
        {"q": term, "max": max_results or config.SEARCH_RESULTS_PER_TERM},
        client,
    )
    return data.get("feeds") or []


def episodes_by_feed(
    feed_id: int,
    since: int | None = None,
    max_results: int = 100,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Episodes for one feed, newest first.

    `since` is a unix timestamp; the API returns only episodes published
    after it. Block 4 passes `user.last_run_at` here.
    """
    params: dict = {"id": feed_id, "max": max_results}
    if since is not None:
        params["since"] = int(since)
    data = _get("/episodes/byfeedid", params, client)
    return data.get("items") or []


if __name__ == "__main__":
    import sys

    term = " ".join(sys.argv[1:]) or "artificial intelligence"
    feeds = search_shows(term, max_results=5)
    print(f'"{term}" -> {len(feeds)} shows')
    for feed in feeds:
        print(f"  {feed.get('title')}  (feed {feed.get('id')})")
