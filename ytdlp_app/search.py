"""YouTube search and URL/playlist resolution via yt-dlp.

We use yt-dlp's built-in `ytsearchN:` extractor and `extract_flat="in_playlist"`
so result lists come back in ~1 second instead of ~30 (which is what happens
if yt-dlp resolves every video's metadata page).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import yt_dlp


ProgressFn = Callable[[str], None]

_URL_RE = re.compile(r"^\s*https?://", re.IGNORECASE)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    uploader: str
    duration_s: int | None
    view_count: int | None
    upload_date: str | None     # YYYYMMDD
    thumbnail_url: str | None
    source: str = "search"      # "search" | "playlist" | "single"

    def display_title(self, max_len: int = 95) -> str:
        if len(self.title) <= max_len:
            return self.title
        return self.title[: max_len - 1] + "…"

    def metadata_line(self) -> str:
        bits: list[str] = []
        if self.uploader:
            bits.append(self.uploader)
        if self.duration_s:
            bits.append(_format_duration(self.duration_s))
        if self.view_count:
            bits.append(f"{_format_count(self.view_count)} views")
        if self.upload_date:
            bits.append(_format_date(self.upload_date))
        return "  ·  ".join(bits)


# ----------------------------- public API --------------------------------- #

def is_url(s: str) -> bool:
    return bool(_URL_RE.match(s or ""))


def search_youtube(
    query: str,
    limit: int = 20,
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    videos_only: bool = True,
    audio_only: bool = False,
) -> list[SearchResult]:
    """Run a YouTube search OR resolve a single URL if `query` looks like one.

    Filters:
        videos_only — drop channel/playlist/live-stream entries (default on).
        audio_only  — drop titles that look like music videos / lives, and
                      prefer auto-generated "* - Topic" channels.
    """
    query = (query or "").strip()
    if not query:
        return []
    if is_url(query):
        progress("Resolving URL...")
        return resolve_urls([query], cookies_path=cookies_path,
                            cancel_event=cancel_event, progress=progress)

    progress(f"Searching YouTube for {query!r}...")
    # Over-fetch a bit when filters are on so the user still sees roughly the
    # requested number of rows after we drop non-matches.
    raw_limit = max(1, int(limit))
    if videos_only or audio_only:
        raw_limit = max(raw_limit, int(raw_limit * 1.5))
    target = f"ytsearch{raw_limit}:{query}"
    info = _extract_info(target, cookies_path=cookies_path,
                         cancel_event=cancel_event)
    results = _entries_to_results(info, source="search", videos_only=videos_only)
    if audio_only:
        results = _filter_audio_only(results)
    return results[: int(limit)] if len(results) > limit else results


def resolve_urls(
    urls: Iterable[str],
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
) -> list[SearchResult]:
    """Expand a list of URLs into individual SearchResults.

    Single-video URLs return one result. Playlist URLs return one result per
    member (with `source="playlist"`). Channel URLs similarly expand.
    """
    out: list[SearchResult] = []
    seen: set[str] = set()
    for raw in urls:
        url = raw.strip()
        if not url or not is_url(url):
            continue
        if cancel_event is not None and cancel_event.is_set():
            break
        progress(f"Resolving {url}")
        try:
            info = _extract_info(url, cookies_path=cookies_path,
                                 cancel_event=cancel_event)
        except yt_dlp.utils.DownloadError as e:
            progress(f"ERROR resolving {url}: {e}")
            continue
        source = "playlist" if info.get("entries") else "single"
        for r in _entries_to_results(info, source=source):
            if r.url in seen:
                continue
            seen.add(r.url)
            out.append(r)
    return out


# ----------------------------- internal ----------------------------------- #

def _extract_info(
    target: str,
    *,
    cookies_path: str | None,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    """Call yt-dlp's extract_info with sane options for cheap metadata."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",   # don't hit per-video pages
        "ignoreerrors": True,
        "noplaylist": False,
        "socket_timeout": 20,
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path

    # We can't really cancel mid-yt-dlp-call without a thread to monitor, so
    # this is best-effort: a cancel_event set BEFORE the call short-circuits,
    # and a cancel_event set DURING the call only interrupts subsequent jobs.
    if cancel_event is not None and cancel_event.is_set():
        return {}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False) or {}
    return info if isinstance(info, dict) else {}


def _entries_to_results(
    info: dict[str, Any],
    *,
    source: str,
    videos_only: bool = False,
) -> list[SearchResult]:
    entries = info.get("entries")
    out: list[SearchResult] = []
    if entries:
        for entry in entries:
            if not entry:
                continue
            if videos_only and not _looks_like_video(entry):
                continue
            r = _entry_to_result(entry, source=source)
            if r is not None:
                out.append(r)
    else:
        # Single video (or non-list response): wrap as one row.
        r = _entry_to_result(info, source="single" if source != "search" else "search")
        if r is not None:
            out.append(r)
    return out


# Title patterns we use for the optional "audio only" heuristic. These are
# best-effort — YouTube doesn't tag uploads, so we look for the language people
# put in their titles.
_AUDIO_HINT_RE = re.compile(
    r"\b(official\s*audio|audio\s*only|lyric(s)?\s*video|lyrics|topic|"
    r"provided\s+to\s+youtube\s+by)\b",
    re.IGNORECASE,
)
_VIDEO_HINT_RE = re.compile(
    # "official", "official hd", "official 4k", "official music" + " video".
    r"(\bofficial\b[\w\s\-]{0,20}\bvideo\b"
    r"|\bmusic\s*video\b"
    r"|\bofficial\s*mv\b|\bmv\b"
    r"|\blive(\s+(at|in|from|performance|concert|version|on))?\b"
    r"|\bperformance\s+video\b"
    r"|\b(tour|concert|reaction)\b"
    r"|\bbehind\s+the\s+scenes\b|\bmaking\s+of\b)",
    re.IGNORECASE,
)


def _looks_like_video(entry: dict[str, Any]) -> bool:
    """Heuristic: True iff the raw yt-dlp entry is an actual watchable video.

    Filters out channel pages, playlists, and entries with no duration metadata
    (often upcoming live streams or yt-dlp probe failures).
    """
    if not isinstance(entry, dict):
        return False
    ie_key = (entry.get("ie_key") or "").lower()
    # Non-video extractor kinds.
    if ie_key in {"youtubetab", "youtubechannel", "youtubeuser",
                  "youtubeplaylist", "youtubesearch"}:
        return False
    etype = entry.get("_type") or "url"
    if etype in {"playlist", "channel", "multi_video"}:
        return False
    if not (entry.get("id") or entry.get("url")):
        return False
    # Live / scheduled streams typically come back with no duration.
    if entry.get("duration") in (None, 0):
        return False
    return True


def _filter_audio_only(results: list[SearchResult]) -> list[SearchResult]:
    """Drop entries whose titles look like music videos / live performances.

    "* - Topic" uploaders are YouTube's auto-generated channels for record
    labels and are kept regardless of title (they're audio-only by design).
    """
    out: list[SearchResult] = []
    for r in results:
        is_topic = r.uploader.endswith(" - Topic")
        if is_topic:
            out.append(r)
            continue
        # If the title flags "official audio" / "lyrics", keep even if it
        # also matches video keywords (e.g. "Audio (Official Video)").
        if _AUDIO_HINT_RE.search(r.title):
            out.append(r)
            continue
        if _VIDEO_HINT_RE.search(r.title):
            continue
        out.append(r)
    return out


def _entry_to_result(entry: dict[str, Any], *, source: str) -> SearchResult | None:
    if not isinstance(entry, dict):
        return None

    url = entry.get("webpage_url") or entry.get("url") or entry.get("original_url")
    video_id = entry.get("id")
    if not url and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    if not url:
        return None

    title = entry.get("title") or entry.get("fulltitle") or video_id or "(no title)"
    uploader = entry.get("uploader") or entry.get("channel") or entry.get("uploader_id") or ""
    duration = entry.get("duration")
    duration_s = int(duration) if isinstance(duration, (int, float)) else None
    view_count = entry.get("view_count")
    view_count = int(view_count) if isinstance(view_count, (int, float)) else None
    upload_date = entry.get("upload_date") if isinstance(entry.get("upload_date"), str) else None
    thumbnail_url = entry.get("thumbnail") or _best_thumbnail(entry.get("thumbnails"))

    return SearchResult(
        url=url,
        title=str(title),
        uploader=str(uploader) if uploader else "",
        duration_s=duration_s,
        view_count=view_count,
        upload_date=upload_date,
        thumbnail_url=thumbnail_url,
        source=source,
    )


def _best_thumbnail(thumbs: Any) -> str | None:
    """Pick a medium-resolution thumbnail from yt-dlp's `thumbnails` list."""
    if not isinstance(thumbs, list) or not thumbs:
        return None
    # Prefer something near 320 wide; otherwise return the last (usually highest res).
    candidates = [t for t in thumbs if isinstance(t, dict) and t.get("url")]
    if not candidates:
        return None
    candidates.sort(key=lambda t: abs(int(t.get("width") or 320) - 320))
    return candidates[0].get("url")


# ---------------------------- formatting helpers -------------------------- #

def _format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B".replace(".0B", "B")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _format_date(yyyymmdd: str) -> str:
    if len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd
