"""YouTube playlist / URL resolution as MusicTrack rows."""

from __future__ import annotations

import re
import threading

from ..search import resolve_urls
from .base import MATCH_READY, MusicTrack, ProgressFn


_YOUTUBE_HOST_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com)\b",
    re.IGNORECASE,
)


class YouTubeSource:
    id = "youtube"
    label = "YouTube"

    def is_url(self, url: str) -> bool:
        return bool(_YOUTUBE_HOST_RE.search(url or ""))

    def resolve_urls(
        self,
        urls: list[str],
        *,
        progress: ProgressFn = lambda msg: None,
        cancel_event: threading.Event | None = None,
        cookies_path: str | None = None,
    ) -> list[MusicTrack]:
        results = resolve_urls(
            urls,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
        )
        return [MusicTrack.from_search_result(r) for r in results]

    def parse_text(self, text: str) -> list[MusicTrack]:
        return []
