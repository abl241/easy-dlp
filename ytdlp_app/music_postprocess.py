"""Post-download enrichment for Music mode (metadata + lyrics)."""

from __future__ import annotations

import threading
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .metadata.itunes import ITunesTrack, albums_match, fetch_artwork, search_track
from .metadata.lyrics import fetch_lyrics
from .metadata.parse import ParsedTrack, parse_youtube_track
from .metadata.tagger import apply_itunes_tags, apply_youtube_fallback_tags

ProgressFn = Callable[[str], None]


@dataclass
class TrackInfo:
    """YouTube-sourced metadata captured during download."""

    title: str = ""
    uploader: str = ""
    parsed_artist: str = ""
    parsed_title: str = ""
    duration_s: int | None = None
    thumbnail_url: str | None = None
    itunes_match: ITunesTrack | None = None
    source_album: str = ""
    source_album_artist: str = ""
    source_track_number: int | None = None
    source_disc_number: int | None = None
    source_cover_url: str | None = None


@dataclass
class PostprocessResult:
    success: bool = True
    message: str = ""
    metadata_source: str = ""
    lyrics_embedded: bool = False
    final_path: str = ""


def process_track(
    path: str,
    *,
    track_info: TrackInfo | None = None,
    enrich_metadata: bool = True,
    download_lyrics: bool = True,
    prefer_explicit: bool = True,
    progress: ProgressFn = lambda msg: None,
    cancel_event: threading.Event | None = None,
) -> PostprocessResult:
    """Apply iTunes metadata, cover art, and optional lyrics to a downloaded MP3."""
    if cancel_event is not None and cancel_event.is_set():
        return PostprocessResult(success=False, message="cancelled")

    file_path = Path(path)
    if not file_path.is_file():
        return PostprocessResult(success=False, message=f"file not found: {path}")

    info = track_info or TrackInfo()
    parsed = ParsedTrack(
        artist=info.parsed_artist or "",
        title=info.parsed_title or "",
    )
    if not parsed.title:
        parsed = parse_youtube_track(info.title, info.uploader)

    metadata_source = ""
    tag_title = parsed.title
    tag_artist = parsed.artist
    tag_album = ""
    duration_s = info.duration_s
    itunes_match: ITunesTrack | None = info.itunes_match
    artwork: bytes | None = None
    shared_cover: bytes | None = None
    if info.source_cover_url:
        shared_cover = _fetch_url(info.source_cover_url)
        if shared_cover:
            progress(
                f"[music] album cover downloaded ({len(shared_cover) // 1024} KB)",
            )

    if enrich_metadata:
        if itunes_match is None:
            progress("[music] looking up metadata…")
            itunes_match = search_track(
                parsed.artist, parsed.title,
                duration_s=duration_s,
                album=info.source_album,
                prefer_explicit=prefer_explicit,
            )
        else:
            progress("[music] applying metadata…")

        if itunes_match and info.source_album and not albums_match(
            info.source_album, itunes_match.album,
        ):
            progress(
                f"[music] iTunes matched '{itunes_match.album}' — "
                f"expected '{info.source_album}'; using album metadata",
            )
            itunes_match = None

        if itunes_match:
            metadata_source = "itunes"
            tag_title = itunes_match.title
            tag_artist = itunes_match.artist
            tag_album = itunes_match.album
            if itunes_match.duration_ms:
                duration_s = itunes_match.duration_ms // 1000
            progress(f"[music] matched: {itunes_match.artist} — {itunes_match.title}")
            artwork = shared_cover or fetch_artwork(itunes_match.artwork_url)
            if artwork:
                progress(f"[music] cover art downloaded ({len(artwork) // 1024} KB)")
            else:
                progress("WARN: cover art download failed — trying YouTube thumbnail")
                artwork = _fetch_url(info.thumbnail_url)
        else:
            metadata_source = "youtube" if not info.source_album else "album"
            progress("[music] no iTunes match — using YouTube metadata")
            artwork = shared_cover or _fetch_url(info.thumbnail_url)
            if artwork:
                progress(f"[music] YouTube thumbnail downloaded ({len(artwork) // 1024} KB)")
            if info.source_album:
                tag_album = info.source_album

    lyrics_plain = ""
    if download_lyrics:
        progress("[music] fetching lyrics…")
        lyrics = fetch_lyrics(
            tag_artist or parsed.artist,
            tag_title or parsed.title,
            album=tag_album,
            duration_s=duration_s,
        )
        if lyrics:
            lyrics_plain = lyrics.plain or _plain_from_synced(lyrics.synced)
            progress("[music] lyrics found")
        else:
            progress("[music] no lyrics found")

    try:
        if itunes_match:
            if not artwork:
                artwork = _fetch_url(info.thumbnail_url or info.source_cover_url)
            apply_itunes_tags(
                file_path, _merge_source_metadata(itunes_match, info),
                artwork=artwork,
                lyrics_plain=lyrics_plain,
            )
        elif enrich_metadata and (
            info.source_album or info.source_track_number is not None
        ):
            if not artwork:
                artwork = _fetch_url(info.thumbnail_url or info.source_cover_url)
            apply_itunes_tags(
                file_path,
                ITunesTrack(
                    artist=tag_artist or parsed.artist,
                    title=tag_title or parsed.title,
                    album=info.source_album,
                    album_artist=info.source_album_artist,
                    year=None,
                    genre=None,
                    track_number=info.source_track_number,
                    disc_number=info.source_disc_number,
                    duration_ms=(duration_s * 1000) if duration_s else None,
                    artwork_url=None,
                ),
                artwork=artwork,
                lyrics_plain=lyrics_plain,
            )
        elif enrich_metadata:
            apply_youtube_fallback_tags(
                file_path, parsed,
                artwork=artwork,
                lyrics_plain=lyrics_plain,
            )
        elif lyrics_plain:
            apply_youtube_fallback_tags(
                file_path, parsed,
                lyrics_plain=lyrics_plain,
            )
    except Exception as e:  # noqa: BLE001
        return PostprocessResult(success=False, message=f"tagging failed: {e}")

    progress("[music] tags written")

    return PostprocessResult(
        success=True,
        metadata_source=metadata_source,
        lyrics_embedded=bool(lyrics_plain),
        final_path=str(file_path),
    )


def _merge_source_metadata(match: ITunesTrack, info: TrackInfo) -> ITunesTrack:
    """Prefer playlist/album source fields over per-song iTunes guesses."""
    album = info.source_album or match.album
    album_artist = info.source_album_artist or match.album_artist
    track_number = (
        info.source_track_number
        if info.source_track_number is not None
        else match.track_number
    )
    disc_number = (
        info.source_disc_number
        if info.source_disc_number is not None
        else match.disc_number
    )
    if (
        album == match.album
        and album_artist == match.album_artist
        and track_number == match.track_number
        and disc_number == match.disc_number
    ):
        return match
    return replace(
        match,
        album=album,
        album_artist=album_artist,
        track_number=track_number,
        disc_number=disc_number,
    )


def _fetch_url(url: str | None) -> bytes | None:
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "easy-dlp/2.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except OSError:
        return None


def _plain_from_synced(synced: str) -> str:
    lines: list[str] = []
    for line in synced.splitlines():
        if line.startswith("["):
            idx = line.find("]")
            if idx != -1:
                lines.append(line[idx + 1 :].strip())
        elif line.strip():
            lines.append(line.strip())
    return "\n".join(lines)
