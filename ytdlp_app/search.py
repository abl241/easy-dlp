"""YouTube search and URL/playlist resolution via yt-dlp.

We use yt-dlp's built-in `ytsearchN:` extractor and `extract_flat="in_playlist"`
so result lists come back in ~1 second instead of ~30 (which is what happens
if yt-dlp resolves every video's metadata page).
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus

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
    use_youtube_music: bool = False,
) -> list[SearchResult]:
    """Run a YouTube search OR resolve a single URL if `query` looks like one.

    Filters:
        videos_only — drop channel/playlist/live-stream entries (default on).
        audio_only  — drop titles that look like music videos / lives, and
                      prefer auto-generated "* - Topic" channels. Ignored when
                      `use_youtube_music` is True (songs tab is already scoped).
        use_youtube_music — search music.youtube.com #songs instead of ytsearch.
    """
    query = (query or "").strip()
    if not query:
        return []
    if is_url(query):
        progress("Resolving URL...")
        return resolve_urls([query], cookies_path=cookies_path,
                            cancel_event=cancel_event, progress=progress)

    raw_limit = max(1, int(limit))
    if use_youtube_music:
        progress(f"Searching YouTube Music for {query!r}...")
        target = _youtube_music_search_url(query)
        info = _extract_info(target, cookies_path=cookies_path,
                             cancel_event=cancel_event)
        results = _entries_to_results(
            info, source="search", videos_only=True,
            allow_missing_duration=True,
        )
        results = results[: int(limit)] if len(results) > limit else results
        return _enrich_flat_results(
            results,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
        )

    progress(f"Searching YouTube for {query!r}...")
    # Over-fetch a bit when filters are on so the user still sees roughly the
    # requested number of rows after we drop non-matches.
    if videos_only or audio_only:
        raw_limit = max(raw_limit, int(raw_limit * 1.5))
    target = f"ytsearch{raw_limit}:{query}"
    info = _extract_info(target, cookies_path=cookies_path,
                         cancel_event=cancel_event)
    results = _entries_to_results(info, source="search", videos_only=videos_only)
    if audio_only:
        results = _filter_audio_only(results)
    return results[: int(limit)] if len(results) > limit else results


def _youtube_music_search_url(query: str) -> str:
    """YouTube Music songs-tab search URL understood by yt-dlp."""
    return f"https://music.youtube.com/search?q={quote_plus(query)}#songs"


def find_preferred_audio_url(
    original: SearchResult,
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    expected_artist: str | None = None,
    expected_title: str | None = None,
    expected_duration_s: int | None = None,
) -> str:
    """Search YouTube for an official-audio upload; fall back to `original.url`."""
    if _is_already_preferred_audio(original) and not (expected_artist or expected_title):
        return original.url

    from .metadata.parse import ParsedTrack, parse_youtube_track

    if expected_artist or expected_title:
        parsed = ParsedTrack(artist=expected_artist or "", title=expected_title or "")
    else:
        parsed = parse_youtube_track(original.title, original.uploader)

    query = " ".join(x for x in (parsed.artist, parsed.title) if x).strip()
    if not query:
        query = original.title

    progress(f"[music] searching audio for {query!r}...")
    candidates = search_youtube(
        query,
        limit=10,
        cookies_path=cookies_path,
        cancel_event=cancel_event,
        progress=progress,
        videos_only=True,
        audio_only=True,
    )
    if not candidates:
        progress("[music] no audio alternative — searching any upload…")
        candidates = search_youtube(
            query,
            limit=10,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            videos_only=True,
            audio_only=False,
        )
    if not candidates:
        progress("[music] no alternative — using original link")
        return original.url

    ref_duration = expected_duration_s or original.duration_s
    best = _pick_best_audio_candidate(
        candidates, original, parsed, expected_duration_s=ref_duration,
    )
    if best is None and not _is_already_preferred_audio(original):
        best = _pick_best_audio_candidate(
            candidates, original, parsed,
            expected_duration_s=ref_duration, min_score=0.45,
        )
    if best is not None and best.url != original.url:
        progress(f"[music] using audio: {best.display_title(70)}")
        return best.url

    progress("[music] no better audio — using original link")
    return original.url


def find_youtube_match_for_track(
    artist: str,
    title: str,
    duration_s: int | None = None,
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    use_youtube_music: bool = False,
    audio_only: bool = True,
) -> SearchResult | None:
    """Search YouTube for the best match for a known track.

    Tries audio-first uploads (Topic channels, official audio), then falls
    back to music videos / any matching upload if nothing passes scoring.
    """
    from .metadata.parse import ParsedTrack

    query = " ".join(x for x in (artist, title) if x).strip()
    if not query:
        return None

    stub = SearchResult(
        url="",
        title=title,
        uploader=artist,
        duration_s=duration_s,
        view_count=None,
        upload_date=None,
        thumbnail_url=None,
    )
    parsed = ParsedTrack(artist=artist, title=title)

    if use_youtube_music:
        progress(f"Searching YouTube Music for {query!r}...")
        candidates = search_youtube(
            query,
            limit=10,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            use_youtube_music=True,
        )
        if candidates:
            best = _pick_best_audio_candidate(
                candidates, stub, parsed,
                expected_duration_s=duration_s,
                min_score=0.45,
            )
            if best is not None:
                return _enrich_flat_results(
                    [best],
                    cookies_path=cookies_path,
                    cancel_event=cancel_event,
                    progress=progress,
                )[0]
        progress("No confident YouTube Music match — trying regular YouTube…")

    for try_audio in (True, False):
        if not try_audio and use_youtube_music:
            break
        label = "audio" if try_audio else "any upload"
        progress(f"Searching YouTube ({label}) for {query!r}...")
        candidates = search_youtube(
            query,
            limit=10,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            videos_only=True,
            audio_only=try_audio and audio_only,
        )
        if not candidates:
            if try_audio:
                progress("No audio uploads found — trying music videos…")
            continue
        min_score = 0.55 if try_audio else 0.45
        best = _pick_best_audio_candidate(
            candidates, stub, parsed,
            expected_duration_s=duration_s,
            min_score=min_score,
        )
        if best is not None:
            if not try_audio:
                progress(f"Using music video: {best.display_title(70)}")
            return best
        if try_audio:
            progress("No confident audio match — trying music videos…")
    return None


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
    allow_missing_duration: bool = False,
) -> list[SearchResult]:
    entries = info.get("entries")
    out: list[SearchResult] = []
    if entries:
        for entry in entries:
            if not entry:
                continue
            if videos_only and not _looks_like_video(
                entry, allow_missing_duration=allow_missing_duration,
            ):
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


def _looks_like_video(entry: dict[str, Any], *, allow_missing_duration: bool = False) -> bool:
    """Heuristic: True iff the raw yt-dlp entry is an actual watchable video.

    Filters out channel pages, playlists, and entries with no duration metadata
    (often upcoming live streams or yt-dlp probe failures). YouTube Music flat
    search results omit duration; pass `allow_missing_duration=True` for those.
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
        return allow_missing_duration and bool(entry.get("id"))
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


def _is_already_preferred_audio(result: SearchResult) -> bool:
    """True when the link already looks like an audio-first upload."""
    if result.uploader.endswith(" - Topic"):
        return True
    if _AUDIO_HINT_RE.search(result.title) and not _VIDEO_HINT_RE.search(result.title):
        return True
    kept = _filter_audio_only([result])
    return bool(kept) and not _VIDEO_HINT_RE.search(result.title)


def _pick_best_audio_candidate(
    candidates: list[SearchResult],
    original: SearchResult,
    parsed: Any,
    *,
    expected_duration_s: int | None = None,
    min_score: float = 0.55,
) -> SearchResult | None:
    best: SearchResult | None = None
    best_score = 0.0
    for c in candidates:
        score = _score_audio_candidate(
            c, parsed, original, expected_duration_s=expected_duration_s,
        )
        if score > best_score:
            best_score = score
            best = c
    if best_score < min_score:
        return None
    return best


def _score_audio_candidate(
    candidate: SearchResult,
    parsed: Any,
    original: SearchResult,
    *,
    expected_duration_s: int | None = None,
) -> float:
    compare_title = candidate.title
    if parsed.artist:
        compare_title = _strip_leading_artist(compare_title, parsed.artist)
    title_score = _token_overlap(compare_title, parsed.title or original.title)
    artist_score = (
        _artist_overlap(candidate.uploader, parsed.artist)
        if parsed.artist
        else 0.4
    )
    if title_score < 0.45:
        return 0.0
    if parsed.artist and artist_score < 0.25:
        return 0.0

    score = title_score * 0.45 + artist_score * 0.35
    if candidate.uploader.endswith(" - Topic"):
        score += 0.15
    if _AUDIO_HINT_RE.search(candidate.title):
        score += 0.05
    ref_duration = expected_duration_s or original.duration_s
    if ref_duration and candidate.duration_s:
        delta = abs(ref_duration - candidate.duration_s)
        if delta <= 3:
            score += 0.15
        elif delta <= 8:
            score += 0.05
        elif delta > 25:
            score -= 0.25
    return max(0.0, min(1.0, score))


def _strip_leading_artist(title: str, artist: str) -> str:
    """Drop a leading 'Artist - Title' prefix common on YouTube uploads."""
    if not artist or not title:
        return title
    title_l = title.lower()
    artist_l = artist.lower()
    for sep in (" - ", " – ", " — ", " | ", ": "):
        prefix = artist_l + sep
        if title_l.startswith(prefix):
            return title[len(artist) + len(sep) :]
    return title


def _token_overlap(a: str, b: str) -> float:
    ta = set(_normalize_tokens(a))
    tb = set(_normalize_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _artist_overlap(candidate: str, query: str) -> float:
    a = _normalize_tokens(query)
    b = _normalize_tokens(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    sa, sb = set(a.split()), set(b.split())
    if sa <= sb or sb <= sa:
        return 0.85
    return _token_overlap(candidate, query)


def _normalize_tokens(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    uploader = (
        entry.get("artist") or entry.get("uploader") or entry.get("channel")
        or entry.get("uploader_id") or ""
    )
    duration = entry.get("duration")
    duration_s = int(duration) if isinstance(duration, (int, float)) else None
    view_count = entry.get("view_count")
    view_count = int(view_count) if isinstance(view_count, (int, float)) else None
    upload_date = entry.get("upload_date") if isinstance(entry.get("upload_date"), str) else None
    thumbnail_url = entry.get("thumbnail") or _best_thumbnail(entry.get("thumbnails"))
    if not thumbnail_url and video_id:
        thumbnail_url = _youtube_thumbnail_url(str(video_id))

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


_VIDEO_ID_RE = re.compile(r"(?:v=|/)([a-zA-Z0-9_-]{11})")


def _youtube_thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _video_id_from_url(url: str) -> str | None:
    match = _VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else None


def _needs_metadata_enrichment(result: SearchResult) -> bool:
    return not (result.uploader or "").strip()


def _extract_flat_video_info(
    video_id: str,
    *,
    cookies_path: str | None,
) -> dict[str, Any]:
    """Lightweight per-video metadata fetch (artist, thumb, duration)."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 15,
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        return info if isinstance(info, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _merge_enriched_result(
    base: SearchResult, info: dict[str, Any],
) -> SearchResult:
    if not info:
        return base
    uploader = (
        info.get("artist") or info.get("uploader") or info.get("channel")
        or base.uploader
    )
    thumbnail_url = (
        info.get("thumbnail")
        or _best_thumbnail(info.get("thumbnails"))
        or base.thumbnail_url
    )
    video_id = info.get("id") or _video_id_from_url(base.url)
    if not thumbnail_url and video_id:
        thumbnail_url = _youtube_thumbnail_url(str(video_id))
    duration = info.get("duration")
    duration_s = base.duration_s
    if isinstance(duration, (int, float)):
        duration_s = int(duration)
    view_count = base.view_count
    raw_views = info.get("view_count")
    if isinstance(raw_views, (int, float)):
        view_count = int(raw_views)
    upload_date = base.upload_date
    if isinstance(info.get("upload_date"), str):
        upload_date = info.get("upload_date")
    return SearchResult(
        url=base.url,
        title=str(info.get("title") or base.title),
        uploader=str(uploader) if uploader else "",
        duration_s=duration_s,
        view_count=view_count,
        upload_date=upload_date,
        thumbnail_url=thumbnail_url,
        source=base.source,
    )


def _enrich_flat_results(
    results: list[SearchResult],
    *,
    cookies_path: str | None,
    cancel_event: threading.Event | None,
    progress: ProgressFn,
) -> list[SearchResult]:
    """Fill missing artist/thumbnail fields from per-video yt-dlp lookups."""
    pending: list[tuple[int, SearchResult]] = [
        (i, r) for i, r in enumerate(results) if _needs_metadata_enrichment(r)
    ]
    if not pending:
        return results
    total = len(pending)
    progress(f"Loading artist info (0/{total})...")
    enriched: dict[int, SearchResult] = {}
    done = 0

    def _enrich_one(item: tuple[int, SearchResult]) -> tuple[int, SearchResult]:
        index, result = item
        video_id = _video_id_from_url(result.url)
        if not video_id:
            return index, result
        info = _extract_flat_video_info(video_id, cookies_path=cookies_path)
        return index, _merge_enriched_result(result, info)

    workers = min(4, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_enrich_one, item) for item in pending]
        for fut in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                break
            index, result = fut.result()
            enriched[index] = result
            done += 1
            progress(f"Loading artist info ({done}/{total})...")

    if not enriched:
        return results
    out = list(results)
    for index, result in enriched.items():
        out[index] = result
    return out


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
