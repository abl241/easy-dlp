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

# Collab markers that almost never appear inside a single artist/band name.
_COLLAB_ARTIST_RE = re.compile(
    r"\s+(?:x|×|vs\.?|with)\s+",
    re.IGNORECASE,
)


def primary_album_artist(artist: str, *, album_artist: str = "") -> str:
    """Resolve Album Artist (TPE2) to a single primary name.

    Apple Music groups library entries by Album Artist. Multi-artist credits
    belong in Artist (TPE1); Album Artist must stay primary-only or Music
    creates separate artist categories.
    """
    candidate = (album_artist or artist or "").strip()
    if not candidate:
        return ""
    return _primary_artist_name(candidate)


def _primary_artist_name(name: str) -> str:
    """Best-effort first/primary artist from a joined credit string."""
    name = (name or "").strip()
    if not name:
        return ""

    feat = _FEAT_ARTIST_RE.search(name)
    if feat:
        name = name[: feat.start()].strip()

    collab = _COLLAB_ARTIST_RE.search(name)
    if collab:
        name = name[: collab.start()].strip()

    # Spotify-style joins: "A, B, C". Preserve band names like
    # "Earth, Wind & Fire" (comma + & with no further ", " list).
    if ", " in name:
        first, rest = name.split(", ", 1)
        if " & " in name and ", " not in rest:
            return name
        return first.strip() or name

    if " & " in name:
        return name.split(" & ", 1)[0].strip() or name

    return name
