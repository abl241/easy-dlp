"""YouTube search and URL/playlist resolution via yt-dlp.

We use yt-dlp's built-in `ytsearchN:` extractor and `extract_flat="in_playlist"`
so result lists come back in ~1 second instead of ~30 (which is what happens
if yt-dlp resolves every video's metadata page).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus

import yt_dlp

from .match_config import get_match_config
from .rate_limit import guard_ytdlp_call
from .runtime import apply_ytdlp_runtime_opts


ProgressFn = Callable[[str], None]
PartialFn = Callable[[str, list["SearchResult"]], None]

_URL_RE = re.compile(r"^\s*https?://", re.IGNORECASE)


class _SilentLogger:
    """Swallow yt-dlp log output (used for background metadata lookups)."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


class _PipeLogger:
    """Route yt-dlp logger output through a progress callback."""

    def __init__(self, progress: ProgressFn) -> None:
        self._progress = progress

    def debug(self, msg: str) -> None:
        if msg:
            self._progress(msg)

    def info(self, msg: str) -> None:
        if msg:
            self._progress(msg)

    def warning(self, msg: str) -> None:
        if msg:
            self._progress(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        if msg:
            self._progress(f"ERROR: {msg}")


def _ytdlp_opts_base(
    *,
    cookies_path: str | None = None,
    verbose: bool = False,
    progress: ProgressFn = lambda msg: None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "skip_download": True,
        "logger": _PipeLogger(progress) if verbose else _SilentLogger(),
        "socket_timeout": 20,
        "verbose": bool(verbose),
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    apply_ytdlp_runtime_opts(opts)
    return opts


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
    kind: str = "track"         # "track" | "album" | "playlist"
    item_count: int | None = None  # for albums/playlists when known
    release_year: int | None = None
    # "explicit" | "clean" | "unknown" — from title labels and/or YTM badges
    content_rating: str = "unknown"

    def display_title(self, max_len: int = 95) -> str:
        if len(self.title) <= max_len:
            return self.title
        return self.title[: max_len - 1] + "…"

    def content_rating_badge(self) -> str | None:
        """Short UI badge for clean/explicit, or None when unknown."""
        if self.kind in ("album", "playlist"):
            return None
        if self.content_rating == "clean":
            return "C"
        if self.content_rating == "explicit":
            return "E"
        return None

    def metadata_line(self) -> str:
        bits: list[str] = []
        if self.kind in ("album", "playlist"):
            label = self.kind.upper()
            if self.item_count:
                bits.append(f"{label} · {self.item_count} tracks")
            else:
                bits.append(label)
        if self.uploader:
            bits.append(self.uploader)
        if self.release_year:
            bits.append(str(self.release_year))
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


def is_youtube_music_watch_url(url: str) -> bool:
    """True for canonical YouTube Music track links (album browse entries)."""
    return "music.youtube.com/watch" in (url or "").lower()


_COLLECTION_TITLE_PREFIX_RE = re.compile(
    r"^(?:album|playlist)\s*-\s*",
    re.IGNORECASE,
)


def normalize_collection_title(title: str) -> str:
    """Strip YouTube Music's 'Album - …' / 'Playlist - …' title prefixes."""
    cleaned = _COLLECTION_TITLE_PREFIX_RE.sub("", (title or "").strip()).strip()
    return cleaned or (title or "").strip()


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
    include_albums: bool = False,
    include_playlists: bool = False,
    album_limit: int = 5,
    verbose: bool = False,
    enrich_from: int = 0,
    enrich: bool = True,
    on_partial: PartialFn | None = None,
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

        track_target = _youtube_music_tab_url(query, "songs")
        track_info = _extract_info(
            track_target,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            playlist_end=raw_limit,
            verbose=verbose,
            progress=progress,
        )
        track_results = _entries_to_results(
            track_info,
            source="search",
            videos_only=True,
            allow_missing_duration=True,
        )[:raw_limit]
        if on_partial and track_results:
            on_partial("tracks", track_results)

        extra_results: list[SearchResult] = []
        album_cap = max(1, int(album_limit)) if include_albums else 0
        if include_albums and album_cap > 0:
            album_target = _youtube_music_tab_url(query, "albums")
            album_info = _extract_info(
                album_target,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                playlist_end=album_cap,
                verbose=verbose,
                progress=progress,
            )
            raw_albums = _entries_to_collection_results(
                album_info,
                source="search",
                kind="album",
                limit=album_cap,
            )
            if enrich and raw_albums:
                raw_albums = _enrich_collection_results(
                    raw_albums,
                    cookies_path=cookies_path,
                    cancel_event=cancel_event,
                    progress=progress,
                    max_workers=2,
                )
            albums, _ = _filter_albums_by_relevance(query, raw_albums)
            extra_results.extend(albums)

        if include_playlists:
            pl_cap = max(1, min(int(album_limit), raw_limit))
            pl_target = _youtube_music_tab_url(query, "playlists")
            pl_info = _extract_info(
                pl_target,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                playlist_end=pl_cap,
                verbose=verbose,
                progress=progress,
            )
            extra_results.extend(_entries_to_collection_results(
                pl_info,
                source="search",
                kind="playlist",
                limit=pl_cap,
            ))

        if on_partial and extra_results:
            on_partial("collections", extra_results)

        results = _merge_tracks_and_collections(track_results, extra_results)

        if not enrich:
            return results
        if extra_results:
            # Album rows are enriched before relevance filtering; playlists are not.
            if include_playlists:
                extra_results = _enrich_collection_results(
                    extra_results,
                    cookies_path=cookies_path,
                    cancel_event=cancel_event,
                    progress=progress,
                    max_workers=2,
                )
                if on_partial:
                    on_partial("collections", extra_results)
            results = _dedupe_results(_merge_tracks_and_collections(track_results, extra_results))
        enrich_from = max(0, min(int(enrich_from), len(track_results)))
        if enrich_from > 0:
            head = results[:enrich_from]
            tail = _enrich_flat_results(
                results[enrich_from: len(track_results)],
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                progress=progress,
            )
            annotated = _annotate_content_ratings(
                head + tail,
                cancel_event=cancel_event,
                progress=progress,
            )
            head = annotated[: len(head)]
            tail = annotated[len(head) :]
            if on_partial and tail:
                enriched_tracks = [
                    r for r in (head + tail + extra_results)
                    if r.kind not in ("album", "playlist")
                ]
                on_partial("enrich_tracks", enriched_tracks)
            return head + tail + extra_results
        if track_results:
            enriched_tracks = _enrich_flat_results(
                track_results,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                progress=progress,
            )
            enriched_tracks = _annotate_content_ratings(
                enriched_tracks,
                cancel_event=cancel_event,
                progress=progress,
            )
            if on_partial:
                on_partial("enrich_tracks", enriched_tracks)
            return _dedupe_results(_merge_tracks_and_collections(enriched_tracks, extra_results))
        return results

    progress(f"Searching YouTube for {query!r}...")
    # Over-fetch a bit when filters are on so the user still sees roughly the
    # requested number of rows after we drop non-matches.
    if videos_only or audio_only:
        raw_limit = max(raw_limit, int(raw_limit * 1.5))
    target = f"ytsearch{raw_limit}:{query}"
    info = _extract_info(target, cookies_path=cookies_path,
                         cancel_event=cancel_event, verbose=verbose, progress=progress)
    results = _entries_to_results(info, source="search", videos_only=videos_only)
    if audio_only:
        results = _filter_audio_only(results)
    results = results[: int(limit)] if len(results) > limit else results
    return _dedupe_results(results)


def _merge_tracks_and_collections(
    tracks: list[SearchResult],
    collections: list[SearchResult],
) -> list[SearchResult]:
    """Interleave albums/playlists into song results (YTM-style, one list)."""
    if not collections:
        return tracks
    if not tracks:
        return collections
    # Show a few songs first, then albums/playlists, then the rest.
    insert_at = min(3, len(tracks))
    return tracks[:insert_at] + collections + tracks[insert_at:]


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    """De-duplicate rows while preserving order.

    YouTube Music tabs may return repeated albums/playlists (variants/regions).
    """
    seen: set[tuple[str, str]] = set()
    out: list[SearchResult] = []
    for r in results:
        key = (r.kind or "track", (r.url or "").strip())
        if not key[1]:
            # Keep url-less items (rare) but don't dedupe them.
            out.append(r)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _score_collection_relevance(query: str, result: SearchResult) -> float:
    """Score album/playlist rows against a search query (title + artist)."""
    title = normalize_collection_title(result.title or "")
    title_score = _token_overlap(query, title)
    artist = (result.uploader or "").strip()
    if artist:
        artist_score = _artist_overlap(artist, query)
        combined = title_score * 0.35 + artist_score * 0.65
        # Same title, different artist (e.g. many albums named "Sweet Boy").
        if title_score >= 0.25 and artist_score < 0.3:
            combined *= 0.25
        return combined
    return title_score * 0.4


def _filter_albums_by_relevance(
    query: str,
    albums: list[SearchResult],
    *,
    min_score: float = 0.22,
    relative_cutoff: float = 0.42,
) -> tuple[list[SearchResult], bool]:
    """Keep leading relevant albums; stop at the first clearly off-topic row.

    Returns (filtered_albums, hit_cutoff). YouTube Music orders albums by
    relevance, so once scores drop we treat the rest as exhausted.
    """
    if not albums:
        return [], False
    scored = [(a, _score_collection_relevance(query, a)) for a in albums]
    best = max(score for _, score in scored)
    out: list[SearchResult] = []
    hit_cutoff = False
    for album, score in scored:
        relevant = score >= min_score and (best <= 0 or score >= best * relative_cutoff)
        if relevant:
            out.append(album)
        else:
            hit_cutoff = True
            break
    return out, hit_cutoff


def search_youtube_more_music(
    query: str,
    *,
    track_offset: int,
    track_count: int,
    album_offset: int = 0,
    album_fetch_count: int = 0,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    verbose: bool = False,
    videos_only: bool = True,
    audio_only: bool = False,
    use_youtube_music: bool = True,
) -> tuple[list[SearchResult], list[SearchResult], bool]:
    """Fetch the next page of music search results (append-style).

    Returns (new_tracks, new_albums, albums_exhausted).
    """
    query = (query or "").strip()
    if not query or is_url(query):
        return [], [], True

    track_offset = max(0, int(track_offset))
    track_count = max(0, int(track_count))
    album_offset = max(0, int(album_offset))
    album_fetch_count = max(0, int(album_fetch_count))

    if not use_youtube_music:
        return _search_youtube_more_regular(
            query,
            track_offset=track_offset,
            track_count=track_count,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            verbose=verbose,
            videos_only=videos_only,
            audio_only=audio_only,
        )

    albums_exhausted = album_fetch_count <= 0

    new_tracks: list[SearchResult] = []
    if track_count > 0:
        end = track_offset + track_count
        progress(f"Loading more songs ({track_offset + 1}–{end})...")
        track_target = _youtube_music_tab_url(query, "songs")
        track_info = _extract_info(
            track_target,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            playlist_start=track_offset + 1,
            playlist_end=end,
            verbose=verbose,
            progress=progress,
        )
        new_tracks = _entries_to_results(
            track_info,
            source="search",
            videos_only=True,
            allow_missing_duration=True,
        )[:track_count]
        if new_tracks:
            new_tracks = _enrich_flat_results(
                new_tracks,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                progress=progress,
            )
            new_tracks = _annotate_content_ratings(
                new_tracks,
                cancel_event=cancel_event,
                progress=progress,
            )

    new_albums: list[SearchResult] = []
    if album_fetch_count > 0:
        start = album_offset + 1
        end = album_offset + album_fetch_count
        progress(f"Loading more albums ({start}–{end})...")
        album_target = _youtube_music_tab_url(query, "albums")
        album_info = _extract_info(
            album_target,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            playlist_start=start,
            playlist_end=end,
            verbose=verbose,
            progress=progress,
        )
        raw_albums = _entries_to_collection_results(
            album_info,
            source="search",
            kind="album",
            limit=album_fetch_count,
        )
        if raw_albums:
            raw_albums = _enrich_collection_results(
                raw_albums,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                progress=progress,
                max_workers=2,
            )
        new_albums, cut_off = _filter_albums_by_relevance(query, raw_albums)
        albums_exhausted = cut_off or len(raw_albums) < album_fetch_count

    return _dedupe_results(new_tracks), _dedupe_results(new_albums), albums_exhausted


def _search_youtube_more_regular(
    query: str,
    *,
    track_offset: int,
    track_count: int,
    cookies_path: str | None,
    cancel_event: threading.Event | None,
    progress: ProgressFn,
    verbose: bool,
    videos_only: bool,
    audio_only: bool,
) -> tuple[list[SearchResult], list[SearchResult], bool]:
    """Paginate regular ytsearch results for the Music tab."""
    if track_count <= 0:
        return [], [], True
    end = track_offset + track_count
    progress(f"Loading more results ({track_offset + 1}–{end})...")
    fetch_limit = end
    if videos_only or audio_only:
        fetch_limit = max(fetch_limit, int(fetch_limit * 1.5))
    results = search_youtube(
        query,
        limit=fetch_limit,
        cookies_path=cookies_path,
        cancel_event=cancel_event,
        progress=progress,
        videos_only=videos_only,
        audio_only=audio_only,
        use_youtube_music=False,
        include_albums=False,
        enrich=False,
    )
    new_tracks = results[track_offset:end]
    tracks_exhausted = len(new_tracks) < track_count
    return _dedupe_results(new_tracks), [], tracks_exhausted


def _youtube_music_tab_url(query: str, tab: str) -> str:
    """YouTube Music search URL with a specific tab (songs, albums, playlists)."""
    tab = (tab or "songs").strip().lower()
    if tab not in {"songs", "albums", "playlists"}:
        tab = "songs"
    return f"https://music.youtube.com/search?q={quote_plus(query)}#{tab}"


def find_preferred_audio_url(
    original: SearchResult,
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    expected_artist: str | None = None,
    expected_title: str | None = None,
    expected_duration_s: int | None = None,
    prefer_explicit: bool = True,
) -> str:
    """Search YouTube for an official-audio upload; fall back to `original.url`."""
    from .metadata.parse import ParsedTrack, detect_content_rating, parse_youtube_track

    rating = detect_content_rating(original.title)
    mismatched_rating = (
        (prefer_explicit and rating == "clean")
        or (not prefer_explicit and rating == "explicit")
    )
    if (
        _is_already_preferred_audio(original)
        and not (expected_artist or expected_title)
        and not mismatched_rating
    ):
        return original.url

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
        candidates, original, parsed,
        expected_duration_s=ref_duration,
        prefer_explicit=prefer_explicit,
    )
    if best is None and not _is_already_preferred_audio(original):
        best = _pick_best_audio_candidate(
            candidates, original, parsed,
            expected_duration_s=ref_duration, min_score=0.45,
            prefer_explicit=prefer_explicit,
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
    match_quality: str = "balanced",
    prefer_explicit: bool = True,
) -> SearchResult | None:
    """Search YouTube for the best match for a known track.

    Tries audio-first uploads (Topic channels, official audio), then falls
    back to music videos / any matching upload if nothing passes scoring.
    """
    from .metadata.parse import ParsedTrack
    from .rate_limit import set_sleep_interval_requests

    cfg = get_match_config(match_quality)
    set_sleep_interval_requests(cfg.sleep_interval_requests)

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

    def _flat_search(
        q: str,
        *,
        ytm: bool = False,
        audio: bool = False,
    ) -> list[SearchResult]:
        return search_youtube(
            q,
            limit=cfg.search_limit,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            videos_only=True,
            audio_only=audio and audio_only,
            use_youtube_music=ytm,
            enrich=False,
        )

    def _pick_with_lazy_enrich(candidates: list[SearchResult], min_score: float) -> SearchResult | None:
        if not candidates:
            return None
        scored = [
            (
                c,
                _score_audio_candidate(
                    c, parsed, stub,
                    expected_duration_s=duration_s,
                    prefer_explicit=prefer_explicit,
                ),
            )
            for c in candidates
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        best_flat, best_score = scored[0]

        if cfg.enrich_top_n <= 0:
            if best_score >= min_score:
                return best_flat
            return None

        if best_score >= cfg.fallback_min_score and cfg.fallback_min_score > 0:
            top = [c for c, s in scored[: cfg.enrich_top_n] if s >= min_score * 0.85]
            if not top:
                top = [best_flat]
            enriched = _enrich_flat_results(
                top,
                cookies_path=cookies_path,
                cancel_event=cancel_event,
                progress=progress,
                max_workers=1,
            )
            return _pick_best_audio_candidate(
                enriched, stub, parsed,
                expected_duration_s=duration_s,
                min_score=min_score,
                prefer_explicit=prefer_explicit,
            )

        top = [c for c, s in scored[: cfg.enrich_top_n] if s > 0]
        if not top:
            return None
        enriched = _enrich_flat_results(
            top,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            progress=progress,
            max_workers=1,
        )
        return _pick_best_audio_candidate(
            enriched, stub, parsed,
            expected_duration_s=duration_s,
            min_score=min_score,
            prefer_explicit=prefer_explicit,
        )

    if use_youtube_music:
        progress(f"Searching YouTube Music for {query!r} ({cfg.name})…")
        candidates = _flat_search(query, ytm=True)
        best = _pick_with_lazy_enrich(candidates, cfg.min_match_score)
        if best is not None:
            return best
        progress("No confident YouTube Music match — trying regular YouTube…")

    for try_audio in (True, False):
        if not try_audio and use_youtube_music:
            break
        label = "audio" if try_audio else "any upload"
        progress(f"Searching YouTube ({label}) for {query!r}…")
        candidates = _flat_search(query, audio=try_audio)
        if not candidates:
            if try_audio:
                progress("No audio uploads found — trying music videos…")
            continue
        min_score = cfg.min_match_score if try_audio else cfg.min_match_score - 0.05
        pick = _pick_with_lazy_enrich(candidates, min_score)
        if pick is not None:
            if not try_audio:
                progress(f"Using music video: {pick.display_title(70)}")
            return pick
        if try_audio:
            progress("No confident audio match — trying music videos…")

    if artist and title and cfg.name != "fast":
        progress(f"Trying official-audio search for {artist!r} — {title!r}…")
        candidates = _flat_search(f"{artist} {title} official audio", audio=True)
        pick = _pick_with_lazy_enrich(candidates, cfg.min_match_score - 0.05)
        if pick is not None:
            return pick
    return None


def resolve_urls(
    urls: Iterable[str],
    *,
    cookies_path: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    verbose: bool = False,
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
                                 cancel_event=cancel_event, verbose=verbose, progress=progress)
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
    playlist_start: int | None = None,
    playlist_end: int | None = None,
    verbose: bool = False,
    progress: ProgressFn = lambda msg: None,
) -> dict[str, Any]:
    """Call yt-dlp's extract_info with sane options for cheap metadata."""
    opts = _ytdlp_opts_base(cookies_path=cookies_path, verbose=verbose, progress=progress)
    opts.update({
        "extract_flat": "in_playlist",   # don't hit per-video pages
        "ignoreerrors": True,
        "noplaylist": False,
    })
    if playlist_start is not None and playlist_start > 0:
        opts["playliststart"] = int(playlist_start)
    if playlist_end is not None and playlist_end > 0:
        opts["playlistend"] = int(playlist_end)

    # We can't really cancel mid-yt-dlp-call without a thread to monitor, so
    # this is best-effort: a cancel_event set BEFORE the call short-circuits,
    # and a cancel_event set DURING the call only interrupts subsequent jobs.
    if cancel_event is not None and cancel_event.is_set():
        return {}

    def _do_extract() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False) or {}
        return info if isinstance(info, dict) else {}

    return guard_ytdlp_call(_do_extract, progress=progress if verbose else (lambda msg: None))


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


def _entries_to_collection_results(
    info: dict[str, Any],
    *,
    source: str,
    kind: str,
    limit: int,
) -> list[SearchResult]:
    """Convert non-track entries (albums/playlists) into SearchResult rows."""
    entries = info.get("entries")
    out: list[SearchResult] = []
    if not isinstance(entries, list) or not entries:
        return out
    want = (kind or "").strip().lower()
    if want not in {"album", "playlist"}:
        want = "playlist"

    for entry in entries:
        if not isinstance(entry, dict) or not entry:
            continue
        r = _entry_to_result(entry, source=source)
        if r is None:
            continue
        count_raw = (
            entry.get("playlist_count")
            or entry.get("n_entries")
            or entry.get("entry_count")
        )
        item_count: int | None = None
        if isinstance(count_raw, (int, float)) and int(count_raw) > 0:
            item_count = int(count_raw)
        out.append(replace(r, kind=want, item_count=item_count))
        if len(out) >= max(1, int(limit)):
            break
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
_COVER_HINT_RE = re.compile(
    r"\b(lullaby|rockabye|cover|tribute|karaoke|instrumental|rendition|"
    r"reimagined|8[\s-]?bit|chiptune|nightcore|ukulele|saxophone|violin|"
    r"piano\s+cover|acoustic\s+cover|kids?\s+version|baby'?s?|"
    r"string\s+quartet|orchestral\s+cover|metal\s+cover|"
    r"in\s+the\s+style\s+of|made\s+popular\s+by)\b",
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
    labels and are kept when the artist looks legitimate (not a cover act).
    """
    out: list[SearchResult] = []
    for r in results:
        if _looks_like_cover_upload(r.title, r.uploader):
            continue
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
    prefer_explicit: bool = True,
) -> SearchResult | None:
    best: SearchResult | None = None
    best_score = 0.0
    for c in candidates:
        score = _score_audio_candidate(
            c, parsed, original,
            expected_duration_s=expected_duration_s,
            prefer_explicit=prefer_explicit,
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
    prefer_explicit: bool = True,
) -> float:
    if _looks_like_cover_upload(candidate.title, candidate.uploader):
        return 0.0

    compare_title = candidate.title
    if parsed.artist:
        compare_title = _strip_leading_artist(compare_title, parsed.artist)
    title_score = _token_overlap(compare_title, parsed.title or original.title)

    uploader = (candidate.uploader or "").strip()
    if not uploader and parsed.artist:
        from .metadata.parse import parse_youtube_track

        guessed = parse_youtube_track(candidate.title, "")
        if guessed.artist:
            uploader = guessed.artist
        artist_score = max(
            _artist_overlap(uploader, parsed.artist),
            0.45 if title_score >= 0.6 else 0.0,
        )
    elif parsed.artist:
        artist_score = _artist_overlap(uploader, parsed.artist)
    else:
        artist_score = 0.4
    if title_score < 0.45:
        return 0.0
    if parsed.artist and artist_score < 0.35:
        return 0.0

    score = title_score * 0.45 + artist_score * 0.35
    if parsed.artist:
        if artist_score >= 0.85:
            score += 0.2
        elif artist_score >= 0.7:
            score += 0.1
    if candidate.uploader.endswith(" - Topic"):
        topic_artist = _topic_channel_artist(candidate.uploader)
        if parsed.artist:
            topic_overlap = _artist_overlap(topic_artist, parsed.artist)
            if topic_overlap >= 0.7:
                score += 0.15
            elif topic_overlap < 0.35:
                return 0.0
        else:
            score += 0.05
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
            score -= 0.35
        elif delta > 15:
            score -= 0.2
    if candidate.view_count and candidate.view_count >= 100_000:
        score += min(0.05, 0.01 * (len(str(candidate.view_count)) - 5))
    score += _content_rating_adjustment(
        candidate.title,
        prefer_explicit=prefer_explicit,
        content_rating=getattr(candidate, "content_rating", "unknown") or "unknown",
    )
    return max(0.0, min(1.0, score))


def _content_rating_adjustment(
    title: str,
    *,
    prefer_explicit: bool,
    content_rating: str = "unknown",
) -> float:
    """Bias search ranking toward explicit or clean uploads when labeled."""
    from .metadata.parse import detect_content_rating

    rating = content_rating if content_rating in ("explicit", "clean") else detect_content_rating(title)
    if rating == "unknown":
        return 0.0
    if prefer_explicit:
        return 0.14 if rating == "explicit" else -0.22
    return 0.14 if rating == "clean" else -0.22


def _topic_channel_artist(uploader: str) -> str:
    suffix = " - Topic"
    if uploader.endswith(suffix):
        return uploader[: -len(suffix)].strip()
    return uploader


def _looks_like_cover_upload(title: str, uploader: str) -> bool:
    blob = f"{title} {uploader}"
    return bool(_COVER_HINT_RE.search(blob))


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

    from .metadata.parse import detect_content_rating

    return SearchResult(
        url=url,
        title=str(title),
        uploader=str(uploader) if uploader else "",
        duration_s=duration_s,
        view_count=view_count,
        upload_date=upload_date,
        thumbnail_url=thumbnail_url,
        source=source,
        content_rating=detect_content_rating(str(title)),
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
    verbose: bool = False,
    progress: ProgressFn = lambda msg: None,
) -> dict[str, Any]:
    """Lightweight per-video metadata fetch (artist, thumb, duration)."""
    opts = _ytdlp_opts_base(cookies_path=cookies_path, verbose=verbose, progress=progress)
    opts.update({
        "extract_flat": True,
        "ignoreerrors": True,
        "socket_timeout": 15,
    })
    url = f"https://www.youtube.com/watch?v={video_id}"

    def _do_extract() -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        return info if isinstance(info, dict) else {}

    try:
        return guard_ytdlp_call(_do_extract, progress=progress if verbose else (lambda msg: None))
    except yt_dlp.utils.DownloadError:
        return {}
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
    title = str(info.get("title") or base.title)
    content_rating = base.content_rating
    if content_rating not in ("explicit", "clean"):
        from .metadata.parse import detect_content_rating

        content_rating = detect_content_rating(title)
    return SearchResult(
        url=base.url,
        title=title,
        uploader=str(uploader) if uploader else "",
        duration_s=duration_s,
        view_count=view_count,
        upload_date=upload_date,
        thumbnail_url=thumbnail_url,
        source=base.source,
        kind=base.kind,
        item_count=base.item_count,
        release_year=base.release_year,
        content_rating=content_rating,
    )


def _merge_enriched_collection_result(
    base: SearchResult,
    info: dict[str, Any],
    *,
    cookies_path: str | None,
) -> SearchResult:
    """Fill album/playlist fields from a playlist-level yt-dlp info dict."""
    if not info:
        return base
    title = normalize_collection_title(str(info.get("title") or base.title))
    uploader = (
        info.get("artist")
        or info.get("uploader")
        or info.get("channel")
        or base.uploader
        or ""
    )
    if not uploader:
        entries = info.get("entries")
        if isinstance(entries, list) and entries:
            first = entries[0] if isinstance(entries[0], dict) else None
            if isinstance(first, dict):
                uploader = (
                    first.get("artist")
                    or first.get("uploader")
                    or first.get("channel")
                    or first.get("uploader_id")
                    or ""
                )
                if not uploader:
                    url0 = first.get("webpage_url") or first.get("url")
                    vid = (
                        str(first.get("id") or "")
                        or (_video_id_from_url(str(url0)) if url0 else "")
                    )
                    if vid:
                        vinfo = _extract_flat_video_info(
                            str(vid),
                            cookies_path=cookies_path,
                            verbose=False,
                            progress=lambda msg: None,
                        )
                        uploader = (
                            vinfo.get("artist")
                            or vinfo.get("uploader")
                            or vinfo.get("channel")
                            or ""
                        )
    thumbnail_url = (
        info.get("thumbnail")
        or _best_thumbnail(info.get("thumbnails"))
        or base.thumbnail_url
    )
    count_raw = info.get("playlist_count") or info.get("n_entries") or info.get("entry_count")
    item_count = base.item_count
    if item_count is None and isinstance(count_raw, (int, float)) and int(count_raw) > 0:
        item_count = int(count_raw)
    year_raw = info.get("release_year") or info.get("year")
    release_year = base.release_year
    if release_year is None and isinstance(year_raw, (int, float)) and int(year_raw) > 0:
        release_year = int(year_raw)

    return SearchResult(
        url=base.url,
        title=title,
        uploader=str(uploader) if uploader else "",
        duration_s=base.duration_s,
        view_count=base.view_count,
        upload_date=base.upload_date,
        thumbnail_url=thumbnail_url,
        source=base.source,
        kind=base.kind,
        item_count=item_count,
        release_year=release_year,
    )


def _enrich_collection_results(
    results: list[SearchResult],
    *,
    cookies_path: str | None,
    cancel_event: threading.Event | None,
    progress: ProgressFn,
    max_workers: int = 2,
) -> list[SearchResult]:
    """Enrich album/playlist rows with artist/cover/count/year metadata."""
    pending: list[tuple[int, SearchResult]] = [
        (i, r)
        for i, r in enumerate(results)
        if r.kind in ("album", "playlist") and r.url
    ]
    if not pending:
        return results
    total = len(pending)
    progress(f"Loading album info (0/{total})...")
    enriched: dict[int, SearchResult] = {}
    done = 0

    def _enrich_one(item: tuple[int, SearchResult]) -> tuple[int, SearchResult]:
        index, base = item
        info = _extract_info(
            base.url,
            cookies_path=cookies_path,
            cancel_event=cancel_event,
            playlist_end=1,
            verbose=False,
            progress=lambda msg: None,
        )
        return index, _merge_enriched_collection_result(base, info, cookies_path=cookies_path)

    workers = min(max(1, int(max_workers)), total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_enrich_one, item) for item in pending]
        for fut in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                break
            index, result = fut.result()
            enriched[index] = result
            done += 1
            progress(f"Loading album info ({done}/{total})...")

    if not enriched:
        return results
    out = list(results)
    for index, r in enriched.items():
        out[index] = r
    return out


def _annotate_content_ratings(
    results: list[SearchResult],
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn = lambda msg: None,
    max_workers: int = 4,
) -> list[SearchResult]:
    """Fill clean/explicit labels via title hints, YTM badges, and twin inference."""
    from .metadata.parse import detect_content_rating

    if not results:
        return results

    out = list(results)
    for i, r in enumerate(out):
        if r.kind in ("album", "playlist"):
            continue
        if r.content_rating in ("explicit", "clean"):
            continue
        rating = detect_content_rating(r.title)
        if rating != "unknown":
            out[i] = replace(r, content_rating=rating)

    pending: list[tuple[int, SearchResult]] = [
        (i, r)
        for i, r in enumerate(out)
        if r.kind not in ("album", "playlist") and r.content_rating == "unknown"
    ]
    if pending:
        total = len(pending)
        progress(f"Checking explicit badges (0/{total})...")
        done = 0

        def _fetch_one(item: tuple[int, SearchResult]) -> tuple[int, str]:
            index, result = item
            video_id = _video_id_from_url(result.url)
            if not video_id:
                return index, "unknown"
            flag = _ytm_is_explicit(video_id)
            if flag is True:
                return index, "explicit"
            if flag is False:
                return index, "checked"
            return index, "unknown"

        workers = min(max(1, max_workers), total)
        checked: set[int] = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_fetch_one, item) for item in pending]
            for fut in as_completed(futures):
                if cancel_event is not None and cancel_event.is_set():
                    break
                index, status = fut.result()
                done += 1
                progress(f"Checking explicit badges ({done}/{total})...")
                if status == "explicit":
                    out[index] = replace(out[index], content_rating="explicit")
                elif status == "checked":
                    checked.add(index)

        out = _infer_clean_twins(out, checked_unknown=checked)

    return out


def _infer_clean_twins(
    results: list[SearchResult],
    *,
    checked_unknown: set[int],
) -> list[SearchResult]:
    """When a song has both an Explicit and unlabeled twin, mark the twin Clean."""
    groups: dict[tuple[str, str, int | None], list[int]] = {}
    for i, r in enumerate(results):
        if r.kind in ("album", "playlist"):
            continue
        key = (
            _normalize_tokens(r.title),
            _normalize_tokens(r.uploader),
            r.duration_s,
        )
        if not key[0]:
            continue
        groups.setdefault(key, []).append(i)

    out = list(results)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        has_explicit = any(out[i].content_rating == "explicit" for i in indexes)
        if not has_explicit:
            continue
        for i in indexes:
            if out[i].content_rating != "unknown":
                continue
            if i not in checked_unknown:
                continue
            out[i] = replace(out[i], content_rating="clean")
    return out


# Public YouTube Music web client key (same one the website embeds).
_YTM_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_YTM_INNERTUBE_CLIENT = {
    "clientName": "WEB_REMIX",
    "clientVersion": "1.20240724.01.00",
    "hl": "en",
    "gl": "US",
}


def _ytm_is_explicit(video_id: str) -> bool | None:
    """Return True if YTM shows an Explicit badge, False if not, None on error."""
    video_id = (video_id or "").strip()
    if not video_id:
        return None
    body = {
        "context": {"client": dict(_YTM_INNERTUBE_CLIENT)},
        "videoId": video_id,
    }
    req = urllib.request.Request(
        (
            "https://music.youtube.com/youtubei/v1/next"
            f"?key={_YTM_INNERTUBE_KEY}&prettyPrint=false"
        ),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.HTTPError, urllib.error.URLError):
        return None
    if "MUSIC_EXPLICIT_BADGE" in payload or '"label":"Explicit"' in payload:
        return True
    # Successful response without a badge — not marked explicit.
    if '"trackingParams"' in payload or "playlistPanelVideoRenderer" in payload:
        return False
    return None


def _enrich_flat_results(
    results: list[SearchResult],
    *,
    cookies_path: str | None,
    cancel_event: threading.Event | None,
    progress: ProgressFn,
    max_workers: int = 4,
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

    workers = min(max(1, max_workers), total)
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
