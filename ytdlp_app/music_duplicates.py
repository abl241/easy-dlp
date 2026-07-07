"""Detect existing music downloads in the output folder or Music library."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from . import apple_music as am
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


DuplicateLocation = Literal["folder", "library", ""]


def check_music_duplicate(
    out_dir: str | Path,
    *,
    result: SearchResult | None = None,
    track: MusicTrack | None = None,
    check_apple_music: bool = False,
) -> tuple[bool, str, DuplicateLocation]:
    """Return whether a track already exists, its display name, and where."""
    if track is not None:
        artist = (track.artist or "").strip()
        title = (track.title or "").strip()
        duration_s = track.duration_s
        album = track.album
        display = track.display_title()
        basename = basename_for_track(track)
    elif result is not None:
        parsed = parse_youtube_track(result.title, result.uploader)
        artist = (parsed.artist or "").strip()
        title = (parsed.title or "").strip()
        duration_s = result.duration_s
        album = ""
        display = result.display_title()
        basename = basename_for_result(result)
    else:
        return False, "", ""

    if music_exists_in_dir(out_dir, basename):
        return True, display, "folder"

    if not check_apple_music or not am.is_supported():
        return False, display, ""

    titles = [title] if title else []
    itunes = search_track(artist, title, duration_s=duration_s, album=album)
    if itunes and itunes.title:
        canon = itunes.title.strip()
        if canon and all(canon.casefold() != t.casefold() for t in titles):
            titles.append(canon)

    for candidate in titles:
        if am.track_exists_in_library(artist, candidate):
            return True, display, "library"

    return False, display, ""


def duplicate_location_label(location: DuplicateLocation, out_dir: str | Path) -> str:
    if location == "library":
        return "your Apple Music library"
    if location == "folder":
        folder = Path(out_dir).name or str(out_dir)
        return folder
    return ""
