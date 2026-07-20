"""Parse YouTube upload titles into artist + track name."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ContentRating = Literal["explicit", "clean", "unknown"]

# Trailing parenthetical/bracket tags common on music uploads.
_JUNK_SUFFIX_RE = re.compile(
    r"\s*[\(\[\{]"
    r"(?:"
    r"official\s*(?:audio|video|lyric\s*video|visualizer|music\s*video)?"
    r"|audio\s*only|lyrics?(?:\s*video)?|visualizer|full\s*album"
    r"|hd|4k|mv|explicit|clean|remaster(?:ed)?"
    r")"
    r"[\)\]\}].*$",
    re.IGNORECASE,
)

# Detect advisory labels before junk stripping removes them.
_EXPLICIT_RATING_RE = re.compile(
    r"[\(\[\{]\s*explicit\s*[\)\]\}]"
    r"|\bexplicit\s*(?:version|edit|lyrics?)?\b"
    r"|\buncensored\b"
    r"|\bdirty\s+version\b",
    re.IGNORECASE,
)
_CLEAN_RATING_RE = re.compile(
    r"[\(\[\{]\s*clean(?:\s*version)?\s*[\)\]\}]"
    r"|\bclean\s+version\b"
    r"|\bcensored\b"
    r"|\bradio\s+edit\b"
    r"|\bnon[\s-]?explicit\b"
    r"|\bedited\s+version\b",
    re.IGNORECASE,
)

# Leading tags like "[Official Audio] Artist - Title"
_JUNK_PREFIX_RE = re.compile(
    r"^[\(\[\{][^\)\]\}]{0,40}[\)\]\}]\s*",
    re.IGNORECASE,
)

_TITLE_SEPARATORS = (" - ", " – ", " — ", " | ", " / ")


@dataclass(frozen=True)
class ParsedTrack:
    artist: str
    title: str


def detect_content_rating(text: str) -> ContentRating:
    """Return explicit/clean/unknown from title or catalog metadata text."""
    raw = (text or "").strip()
    if not raw:
        return "unknown"
    # Check clean first so "Clean (Explicit)" quirks rarely matter; real
    # titles almost never combine both, and "clean" markers are rarer.
    if _CLEAN_RATING_RE.search(raw):
        return "clean"
    if _EXPLICIT_RATING_RE.search(raw):
        return "explicit"
    return "unknown"


def parse_youtube_track(raw_title: str, uploader: str = "") -> ParsedTrack:
    """Best-effort split of a YouTube music upload into artist and track title."""
    title = _JUNK_PREFIX_RE.sub("", (raw_title or "").strip())
    uploader = (uploader or "").strip()

    if uploader.endswith(" - Topic"):
        artist = uploader[: -len(" - Topic")].strip()
        track = _clean_track_title(title)
        return ParsedTrack(artist=artist or uploader, title=track or title)

    for sep in _TITLE_SEPARATORS:
        if sep in title:
            left, right = title.split(sep, 1)
            artist = left.strip()
            track = _clean_track_title(right.strip())
            if artist and track:
                return ParsedTrack(artist=artist, title=track)

    track = _clean_track_title(title)
    artist = ""
    if uploader and not uploader.lower().startswith("youtube"):
        artist = uploader
    return ParsedTrack(artist=artist, title=track or title)


def _clean_track_title(title: str) -> str:
    prev = None
    cleaned = title.strip()
    while cleaned and cleaned != prev:
        prev = cleaned
        cleaned = _JUNK_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00]')


def sanitize_filename(name: str, *, max_len: int = 180) -> str:
    """Filesystem-safe name that keeps spaces (no restrictfilenames underscores)."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or "track"


_FEAT_ARTIST_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+",
    re.IGNORECASE,
)


def primary_album_artist(artist: str, *, album_artist: str = "") -> str:
    """Resolve the Album Artist (TPE2) value for library grouping."""
    album_artist = (album_artist or "").strip()
    if album_artist:
        return album_artist
    artist = (artist or "").strip()
    if not artist:
        return ""
    if "&" in artist:
        return artist.split("&", 1)[0].strip()
    feat = _FEAT_ARTIST_RE.search(artist)
    if feat:
        return artist[: feat.start()].strip()
    return artist
