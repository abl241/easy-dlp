"""Write ID3 tags and cover art to MP3 files."""

from __future__ import annotations

from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPOS, TRCK, USLT, ID3NoHeaderError
from mutagen.mp3 import MP3

from .itunes import ITunesTrack
from .parse import ParsedTrack


def _mime_for_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def _clear_frames(tags: ID3, *frame_ids: str) -> None:
    for frame_id in frame_ids:
        tags.delall(frame_id)


def _set_cover_art(tags: ID3, artwork: bytes) -> None:
    """Embed front-cover art using ID3v2.3-friendly APIC settings."""
    for key in list(tags.keys()):
        if key.startswith("APIC"):
            del tags[key]
    tags["APIC:Cover"] = APIC(
        encoding=0,
        mime=_mime_for_image(artwork),
        type=3,
        desc="Cover",
        data=artwork,
    )


def _save(audio: MP3) -> None:
    # ID3v2.3 has the broadest player compatibility (macOS Music, etc.).
    audio.save(v2_version=3)


def apply_itunes_tags(
    path: str | Path,
    track: ITunesTrack,
    *,
    artwork: bytes | None,
    lyrics_plain: str = "",
) -> None:
    """Write iTunes-sourced metadata (and optional lyrics) to an MP3 file."""
    path = Path(path)
    audio = MP3(path, ID3=ID3)
    try:
        audio.tags
    except ID3NoHeaderError:
        audio.add_tags()

    tags = audio.tags
    _clear_frames(tags, "TIT2", "TPE1", "TALB", "TDRC", "TCON", "TRCK", "TPOS", "USLT")

    tags["TIT2"] = TIT2(encoding=3, text=track.title)
    tags["TPE1"] = TPE1(encoding=3, text=track.artist)
    if track.album:
        tags["TALB"] = TALB(encoding=3, text=track.album)
    if track.year:
        tags["TDRC"] = TDRC(encoding=3, text=str(track.year))
    if track.genre:
        tags["TCON"] = TCON(encoding=3, text=track.genre)
    if track.track_number:
        # TRCK is "track" or "track/total" — not "disc/track".
        tags["TRCK"] = TRCK(encoding=3, text=str(track.track_number))
    if track.disc_number:
        tags["TPOS"] = TPOS(encoding=3, text=str(track.disc_number))

    if artwork:
        _set_cover_art(tags, artwork)

    if lyrics_plain:
        tags["USLT::eng"] = USLT(encoding=3, lang="eng", desc="", text=lyrics_plain)

    _save(audio)


def apply_youtube_fallback_tags(
    path: str | Path,
    parsed: ParsedTrack,
    *,
    artwork: bytes | None = None,
    lyrics_plain: str = "",
) -> None:
    """Write best-effort tags from YouTube parsing when iTunes has no match."""
    path = Path(path)
    audio = MP3(path, ID3=ID3)
    try:
        audio.tags
    except ID3NoHeaderError:
        audio.add_tags()

    tags = audio.tags
    tags["TIT2"] = TIT2(encoding=3, text=parsed.title)
    if parsed.artist:
        tags["TPE1"] = TPE1(encoding=3, text=parsed.artist)
    if artwork:
        _set_cover_art(tags, artwork)
    if lyrics_plain:
        tags["USLT::eng"] = USLT(encoding=3, lang="eng", desc="", text=lyrics_plain)

    _save(audio)
