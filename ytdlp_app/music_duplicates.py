"""Detect existing music downloads in the output folder."""

from __future__ import annotations

import re
from pathlib import Path

from .metadata.itunes import search_track
from .metadata.parse import parse_youtube_track, sanitize_filename
from .search import SearchResult
from .sources.base import MusicTrack

_NUMBERED_STEM_RE = re.compile(r"^(.+) \(\d+\)$")


def predict_music_basename(
    artist: str,
    title: str,
    *,
    duration_s: int | None = None,
    album: str = "",
) -> str:
    """Guess the MP3 filename stem download_music would use."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title and not artist:
        return "track"
    itunes = search_track(artist, title, duration_s=duration_s, album=album)
    name = itunes.title if itunes and itunes.title else title
    return sanitize_filename(name or title or artist or "track")


def basename_for_track(track: MusicTrack) -> str:
    return predict_music_basename(
        track.artist,
        track.title,
        duration_s=track.duration_s,
        album=track.album,
    )


def basename_for_result(result: SearchResult) -> str:
    parsed = parse_youtube_track(result.title, result.uploader)
    return predict_music_basename(
        parsed.artist,
        parsed.title,
        duration_s=result.duration_s,
    )


def music_exists_in_dir(out_dir: str | Path, basename: str) -> bool:
    """True if an MP3 with this stem (or numbered variant) exists in `out_dir`."""
    directory = Path(out_dir)
    if not directory.is_dir():
        return False
    target = (basename or "").casefold()
    if not target:
        return False
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() != ".mp3":
            continue
        stem = path.stem
        match = _NUMBERED_STEM_RE.match(stem)
        if match:
            stem = match.group(1)
        if stem.casefold() == target:
            return True
    return False
