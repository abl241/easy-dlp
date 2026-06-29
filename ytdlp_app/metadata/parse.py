"""Parse YouTube upload titles into artist + track name."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
