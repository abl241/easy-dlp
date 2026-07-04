"""Unified music-track model and source protocol for playlist import."""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, replace
from typing import Callable, Protocol

from ..search import SearchResult, _format_duration


ProgressFn = Callable[[str], None]

MATCH_READY = "ready"
MATCH_PENDING = "pending"
MATCH_MATCHED = "matched"
MATCH_FAILED = "failed"


@dataclass
class MusicTrack:
    artist: str
    title: str
    duration_s: int | None = None
    album: str = ""
    cover_url: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    source: str = "youtube"
    source_url: str | None = None
    youtube_url: str | None = None
    youtube_title: str = ""
    youtube_uploader: str = ""
    thumbnail_url: str | None = None
    match_status: str = MATCH_PENDING

    def display_title(self, max_len: int = 95) -> str:
        text = f"{self.artist} — {self.title}" if self.artist else self.title
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def metadata_line(self) -> str:
        bits: list[str] = []
        if self.album:
            bits.append(self.album)
        if self.duration_s:
            bits.append(_format_duration(self.duration_s))
        if self.source and self.source != "youtube":
            bits.append(self.source.capitalize())
        if self.match_status == MATCH_FAILED:
            bits.append("no YouTube match")
        elif self.match_status == MATCH_PENDING:
            bits.append("needs YouTube match")
        elif self.youtube_uploader:
            bits.append(self.youtube_uploader)
        return "  ·  ".join(bits)

    def is_downloadable(self) -> bool:
        return bool(self.youtube_url) and self.match_status in (MATCH_READY, MATCH_MATCHED)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MusicTrack:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @classmethod
    def from_search_result(cls, result: SearchResult) -> MusicTrack:
        return cls(
            artist=result.uploader,
            title=result.title,
            duration_s=result.duration_s,
            source="youtube",
            source_url=result.url,
            youtube_url=result.url,
            youtube_title=result.title,
            youtube_uploader=result.uploader,
            thumbnail_url=result.thumbnail_url,
            match_status=MATCH_READY,
        )

    def to_search_result(self) -> SearchResult | None:
        if not self.youtube_url:
            return None
        return SearchResult(
            url=self.youtube_url,
            title=self.youtube_title or self.title,
            uploader=self.youtube_uploader or self.artist,
            duration_s=self.duration_s,
            view_count=None,
            upload_date=None,
            thumbnail_url=self.thumbnail_url or self.cover_url,
            source=self.source,
        )

    def with_match(self, result: SearchResult | None) -> MusicTrack:
        if result is None:
            return replace(self, match_status=MATCH_FAILED)
        return replace(
            self,
            youtube_url=result.url,
            youtube_title=result.title,
            youtube_uploader=result.uploader,
            thumbnail_url=result.thumbnail_url or self.cover_url,
            duration_s=self.duration_s or result.duration_s,
            match_status=MATCH_MATCHED,
        )


@dataclass(frozen=True)
class PlatformConfig:
    id: str
    label: str
    placeholder: str
    hint: str
    supports_text_fallback: bool
    needs_youtube_match: bool


PLATFORM_CONFIGS: dict[str, PlatformConfig] = {
    "youtube": PlatformConfig(
        id="youtube",
        label="YouTube",
        placeholder="https://youtube.com/playlist?list=...",
        hint="Paste video, playlist, or channel URLs — one per line.",
        supports_text_fallback=False,
        needs_youtube_match=False,
    ),
    "spotify": PlatformConfig(
        id="spotify",
        label="Spotify",
        placeholder="https://open.spotify.com/playlist/...",
        hint="Paste a public playlist, album, or track URL.",
        supports_text_fallback=True,
        needs_youtube_match=True,
    ),
}


class MusicSource(Protocol):
    id: str
    label: str

    def is_url(self, url: str) -> bool: ...

    def resolve_urls(
        self,
        urls: list[str],
        *,
        progress: ProgressFn = lambda msg: None,
        cancel_event: threading.Event | None = None,
    ) -> list[MusicTrack]: ...

    def parse_text(self, text: str) -> list[MusicTrack]: ...


# "Artist - Title", "Artist — Title", "Artist / Title"
_TEXT_LINE_RE = re.compile(
    r"^\s*(?P<artist>.+?)\s*(?:[-–—/]| feat\.? )\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)


def parse_track_list_text(text: str, *, source: str = "text") -> list[MusicTrack]:
    tracks: list[MusicTrack] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _TEXT_LINE_RE.match(line)
        if not m:
            continue
        tracks.append(
            MusicTrack(
                artist=m.group("artist").strip(),
                title=m.group("title").strip(),
                source=source,
                match_status=MATCH_PENDING,
            )
        )
    return tracks
