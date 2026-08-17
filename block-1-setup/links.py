"""Human-readable episode links (ARCHITECTURE section 6, stage 4).

A digest exists to help someone decide what to play, so every link in it must
open a page a human can read: the episode title, the show, the notes. A raw
enclosure URL fails that test — it opens a bare audio player or starts a
download with no context at all.

Podcast Index separates the two cleanly. `link` is the episode's webpage and
`enclosureUrl` is the audio file; only the first belongs in `episode.web_url`.

The complication is coverage. In a live 1,000-episode sample across five
categories, 545 episodes carried no `link` at all. Reading one field is
therefore not enough, and falling back to `enclosureUrl` — which this pipeline
originally did — put an audio file in the majority of stored links. Hence the
ladder in `episode_page_url()`, and `safe_page_url()` as a second, independent
guard at render time so a row written before this fix can never be emailed as
a link.
"""

import urllib.parse

import config

# Extensions that mean "this is the media file, not a page about it". Podcast
# audio is nearly always mp3 or m4a; the rest cover video shows and the few
# feeds that publish ogg/opus.
MEDIA_SUFFIXES = frozenset(
    {
        ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus",
        ".wav", ".flac", ".wma", ".mp4", ".m4v", ".mov", ".webm",
    }
)

WEB_SCHEMES = frozenset({"http", "https"})

# Apple's public show page. It needs no API key, takes the bare iTunes id that
# Podcast Index already returns on every episode, and redirects to the
# localized canonical URL. `feedItunesId` was present on 543 of the 545
# link-less episodes in the sample above, which is what makes this a real
# fallback tier rather than a token gesture.
APPLE_SHOW_URL = "https://podcasts.apple.com/podcast/id{itunes_id}"


def _suffix(url: str) -> str:
    """Lowercased file extension of the URL path, ignoring query and fragment.

    Splitting first matters: audio hosts append tracking parameters
    (`.../episode.mp3?updated=1712`), so testing the raw string would miss
    them. Comparing against the last slash keeps a dot in a directory name
    from being read as an extension.
    """
    path = urllib.parse.urlsplit(url).path
    dot = path.rfind(".")
    return path[dot:].lower() if dot > path.rfind("/") else ""


def is_media_url(url: str | None) -> bool:
    """True when `url` points at a media file rather than a webpage."""
    if not url:
        return False
    return _suffix(url.strip()) in MEDIA_SUFFIXES


def is_page_url(url: str | None) -> bool:
    """True when `url` is an http(s) address that is not a media file.

    The scheme and host checks reject relative paths and anything exotic a
    feed may have put in `<link>`; both would render as a dead link in mail.
    """
    if not url:
        return False
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() not in WEB_SCHEMES or not parts.netloc:
        return False
    return not is_media_url(url)


def safe_page_url(url: str | None) -> str:
    """`url` when it is a readable webpage, `''` otherwise.

    The render-time guard. It repeats the ingest check on purpose: rows stored
    before this module existed hold enclosure URLs, and the invariant that
    matters is that no audio file is ever *sent*, not merely that none is
    stored.
    """
    url = (url or "").strip()
    return url if is_page_url(url) else ""


def apple_show_url(itunes_id) -> str:
    """Apple's show page for a Podcast Index `feedItunesId`, or `''`."""
    text = str(itunes_id or "").strip()
    if not text.isdigit() or int(text) <= 0:
        return ""
    return APPLE_SHOW_URL.format(itunes_id=text)


def episode_page_url(link=None, itunes_id=None, enclosure_url=None) -> str:
    """Pick the best human-readable URL for one episode.

    1. the feed's own episode page, when it is a page and not the audio file;
    2. the show's Apple page, which at least names the show and lists its
       episodes;
    3. nothing — the digest then prints the title as plain text.

    Never the enclosure. An episode with no readable page is a small loss; a
    digest that opens a download instead of a page is a broken product.

    The `link == enclosure` comparison catches feeds that copy the audio URL
    into `<link>`. Those are invisible to the extension test whenever the host
    serves audio from an extension-less path.
    """
    candidate = (link or "").strip()
    enclosure = (enclosure_url or "").strip()
    if candidate and candidate != enclosure and is_page_url(candidate):
        return candidate
    return apple_show_url(itunes_id)


def site_url(path: str) -> str:
    """Absolute URL on the public onboarding host."""
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def unsubscribe_url(token: str) -> str:
    return site_url(f"unsubscribe/{token}")


def confirmation_url(token: str) -> str:
    return site_url(f"confirm/{token}")
