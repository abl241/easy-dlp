"""Concurrent job queue for downloads and embed tasks.

A `JobQueue` owns separate bounded pools for downloads, tagging, search,
and playlist matching. Search never waits behind a download (and vice
versa). Callers `enqueue(...)` a `Job`; the queue runs the appropriate
function from `downloader` / `embed` / `search`. Job state mutations are
delivered to a single listener callback which marshals them to the Tk
main thread.
"""

from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any, Callable, Optional

from . import apple_music as am
from . import downloader as dl
from . import embed as em
from . import music_postprocess as mp


# ---------------------------- public data model --------------------------- #

# State labels are plain strings so they can be tested + serialized easily.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# Jobs that must not compete with download workers for slots.
_SEARCH_KINDS = frozenset({
    "search",
    "search_more",
    "resolve",
    "source_resolve",
    "source_match_all",
})
_DEFAULT_SEARCH_WORKERS = 2


@dataclass
class Job:
    """One unit of background work tracked by the JobQueue.

    `kind` controls which function in `downloader` / `embed` will be called.
    `params` is a kind-specific dict (see `JobQueue._run_job`).
    """

    id: int
    kind: str                              # "audio" | "video" | "thumb" | "embed_single" | "embed_folder" | "search"
    label: str                             # user-visible: "MP3: Title — Uploader"
    params: dict[str, Any]                 # function args
    state: str = QUEUED
    progress_pct: float = 0.0              # 0..100 (NaN-safe: clamped)
    progress_msg: str = ""
    error: str = ""
    result: Any = None                     # for search jobs, the list of SearchResult
    cancel_event: threading.Event = field(default_factory=threading.Event)
    timings: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_terminal(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)

    @property
    def is_active(self) -> bool:
        return self.state in (QUEUED, RUNNING)


# `Listener(job)` is called any time `job` mutates. It is invoked on a worker
# thread; the GUI listener is expected to push the job into a queue for the
# main thread to consume.
Listener = Callable[[Job], None]


# ------------------------------- the queue ------------------------------- #

class JobQueue:
    def __init__(
        self,
        max_parallel: int,
        listener: Listener,
        *,
        max_search_parallel: int = _DEFAULT_SEARCH_WORKERS,
    ) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._max_search_parallel = max(1, int(max_search_parallel))
        self._listener = listener
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_parallel,
            thread_name_prefix="ytdlp-dl",
        )
        self._search_executor = ThreadPoolExecutor(
            max_workers=self._max_search_parallel,
            thread_name_prefix="ytdlp-search",
        )
        self._postprocess_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ytdlp-tags",
        )
        self._match_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ytdlp-match",
        )
        self._retired_executors: list[ThreadPoolExecutor] = []
        self._closing = False
        self._id_seq = itertools.count(1)
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._futures: dict[int, Future] = {}
        self._output_owners: dict[str, int] = {}

    # --------- pool configuration --------- #

    def set_max_parallel(self, n: int) -> None:
        """Change download worker count. Search pool is unaffected.
        The old executor finishes its current jobs in the background;
        new download jobs go to the new executor."""
        n = max(1, int(n))
        if n == self._max_parallel:
            return
        old = self._executor
        self._max_parallel = n
        self._executor = ThreadPoolExecutor(
            max_workers=n, thread_name_prefix="ytdlp-dl",
        )
        # Don't wait on old jobs here — they keep running on the old pool.
        self._retired_executors.append(old)
        old.shutdown(wait=False)

    def _pool_for(self, kind: str) -> ThreadPoolExecutor:
        if kind in _SEARCH_KINDS:
            return self._search_executor
        return self._executor

    # --------- enqueue + cancel --------- #

    def enqueue(self, kind: str, label: str, **params: Any) -> Job:
        with self._lock:
            if self._closing:
                raise RuntimeError("Job queue is shut down")
            job = Job(
                id=next(self._id_seq),
                kind=kind,
                label=label,
                params=params,
            )
            self._jobs[job.id] = job
        self._notify(job)
        # submit() may run immediately on a free worker thread.
        fut = self._pool_for(kind).submit(self._run_job, job)
        with self._lock:
            self._futures[job.id] = fut
        return job

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.is_terminal:
            return
        job.cancel_event.set()
        if job.state == QUEUED:
            job.state = CANCELLED
            self._notify(job)

    def cancel_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.is_active:
                self.cancel(job.id)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._closing = True
        self.cancel_all()
        self._executor.shutdown(wait=wait)
        for executor in self._retired_executors:
            executor.shutdown(wait=wait)
        self._search_executor.shutdown(wait=wait)
        self._match_executor.shutdown(wait=wait, cancel_futures=True)
        self._postprocess_executor.shutdown(wait=wait)

    # --------- inspection --------- #

    def active(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.is_active]

    def recent(self, n: int = 10) -> list[Job]:
        """Last `n` terminal jobs, most-recent first."""
        with self._lock:
            terms = [j for j in self._jobs.values() if j.is_terminal]
        terms.sort(key=lambda j: j.id, reverse=True)
        return terms[:n]

    def clear_recent(self) -> None:
        with self._lock:
            self._jobs = {jid: j for jid, j in self._jobs.items() if j.is_active}

    # --------- internal --------- #

    def _notify(self, job: Job) -> None:
        try:
            self._listener(job)
        except Exception:  # noqa: BLE001 — never let listener break worker
            pass

    def _reserve_output(self, job: Job, path: str) -> None:
        # Keep one owner until tagging/import finishes, even across workers.
        # Case folding is conservative on case-sensitive filesystems and also
        # protects the usual case-insensitive macOS output directory.
        key = str(Path(path).resolve()).casefold()
        while True:
            if job.cancel_event.is_set():
                raise _Cancelled()
            with self._lock:
                if self._closing:
                    raise _Cancelled()
                owner = self._output_owners.get(key)
                if owner is None or owner == job.id:
                    self._output_owners[key] = job.id
                    return
            if job.cancel_event.wait(0.1):
                raise _Cancelled()

    @contextmanager
    def _timed_stage(self, job: Job, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            job.timings[stage] = job.timings.get(stage, 0.0) + time.monotonic() - started

    def _run_job(self, job: Job, continuation: Callable[[], None] | None = None) -> None:
        handed_off = False
        try:
            if job.cancel_event.is_set():
                raise _Cancelled()
            if continuation is None:
                job.timings["queue"] = time.monotonic() - job.created_at
                job.state = RUNNING
                job.progress_msg = "Starting..."
                self._notify(job)
                with self._timed_stage(job, "work"):
                    next_stage = self._dispatch(job)
                if next_stage is not None:
                    if job.cancel_event.is_set():
                        raise _Cancelled()
                    job.progress_msg = "Waiting for tagging…"
                    self._notify(job)
                    # The continuation owns completion from here. Never mark a
                    # downloaded track DONE until tagging/import has finished.
                    with self._lock:
                        if self._closing:
                            raise _Cancelled()
                        self._postprocess_executor.submit(self._run_job, job, next_stage)
                        handed_off = True
                    return
            else:
                continuation()
        except _Cancelled:
            job.state = CANCELLED
            job.progress_msg = "Cancelled."
        except Exception as e:  # noqa: BLE001
            if job.cancel_event.is_set():
                job.state = CANCELLED
                job.progress_msg = "Cancelled."
            else:
                job.state = FAILED
                job.error = f"{type(e).__name__}: {e}"
                job.progress_msg = job.error
        else:
            if job.cancel_event.is_set():
                job.state = CANCELLED
                job.progress_msg = "Cancelled."
            elif job.state != FAILED:
                job.state = DONE
                job.progress_msg = "Done."
            else:
                job.progress_msg = job.error or "Failed."
        finally:
            if not handed_off:
                with self._lock:
                    self._output_owners = {
                        key: owner for key, owner in self._output_owners.items()
                        if owner != job.id
                    }
                job.timings["total"] = time.monotonic() - job.created_at
                if job.state == DONE:
                    detail = ", ".join(
                        f"{name} {seconds:.1f}s" for name, seconds in job.timings.items()
                        if name not in ("work", "total")
                    )
                    job.progress_msg = f"Done in {job.timings['total']:.1f}s ({detail})"
                self._notify(job)

    def _dispatch(self, job: Job) -> Callable[[], None] | None:
        """Translate Job.kind into a concrete function call."""
        def on_progress(pct: float, msg: str) -> None:
            if job.cancel_event.is_set():
                raise _Cancelled()
            new_pct = _clamp_pct(pct)
            pct_changed = abs(new_pct - job.progress_pct) >= 1.0
            job.progress_pct = new_pct
            if msg:
                if msg != job.progress_msg:
                    job.progress_msg = msg
                    self._notify(job)
            elif pct_changed:
                self._notify(job)

        def log(msg: str) -> None:
            if job.cancel_event.is_set():
                raise _Cancelled()
            if msg != job.progress_msg:
                job.progress_msg = msg
                self._notify(job)

        params = job.params
        cookies = params.get("cookies_path") or None
        verbose = bool(params.get("verbose", False))

        if job.kind == "audio":
            result = dl.download_audio(
                [params["url"]], params["output_dir"],
                cookies_path=cookies,
                verbose=verbose,
                progress=log,
                on_pct=on_progress,
                cancel_event=job.cancel_event,
            )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"

        elif job.kind == "video":
            result = dl.download_video(
                [params["url"]], params["output_dir"],
                cookies_path=cookies,
                verbose=verbose,
                progress=log,
                on_pct=on_progress,
                cancel_event=job.cancel_event,
            )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"

        elif job.kind == "thumb":
            result = dl.download_thumbnails_only(
                [params["url"]], params["output_dir"],
                cookies_path=cookies,
                verbose=verbose,
                progress=log,
                on_pct=on_progress,
                cancel_event=job.cancel_event,
            )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"

        elif job.kind == "music":
            from . import search as se

            if params.get('match_before_download'):
                from .sources.base import MusicTrack
                source = MusicTrack.from_dict(params['tracks'][0])
                if params.get('skip_existing'):
                    from .music_duplicates import check_music_duplicate
                    import sys
                    log('Checking for an existing copy…')
                    exists, display, _ = check_music_duplicate(
                        params['output_dir'], track=source, check_apple_music=sys.platform == 'darwin')
                    if job.cancel_event.is_set():
                        raise _Cancelled()
                    if exists:
                        job.result = {'skipped_duplicate': True}
                        job.label = f'Already in library/folder: {display}'
                        return None
                with self._timed_stage(job, 'matching'):
                    self._match_tracks(job, log)
                if job.cancel_event.is_set():
                    raise _Cancelled()
                matched = job.result[0] if job.result else None
                if matched is None or not matched.is_downloadable():
                    raise ValueError('No reliable YouTube match found. Retry or choose a source in Music search.')
                params['url'] = matched.youtube_url
                params['skip_prefer_audio_rematch'] = True
            url = params["url"]
            # User-picked search results already chose a URL — rematching
            # only delays the download. Spotify/auto matches still rematch
            # when Prefer audio is on.
            with self._timed_stage(job, "audio_selection"):
                skip_rematch = bool(params.get("skip_prefer_audio_rematch"))
                if params.get("prefer_audio") and not skip_rematch:
                    source_url = str(params.get("source_url") or url)
                    prefer_explicit = bool(params.get("allow_explicit", True))
                    skip_rematch = (
                        se.is_youtube_music_watch_url(source_url)
                        or se.is_youtube_music_watch_url(url)
                    )
                    # Still rematch when the chosen upload is the wrong content
                    # rating vs the user's explicit/clean preference.
                    if skip_rematch:
                        from .metadata.parse import detect_content_rating

                        title_hint = str(params.get("source_title") or "")
                        rating = detect_content_rating(title_hint)
                        if (
                            (prefer_explicit and rating == "clean")
                            or (not prefer_explicit and rating == "explicit")
                        ):
                            skip_rematch = False
                    if not skip_rematch:
                        orig = se.SearchResult(
                            url=params.get("source_url") or url,
                            title=params.get("source_title") or "",
                            uploader=params.get("source_uploader") or "",
                            duration_s=params.get("source_duration_s"),
                            view_count=None,
                            upload_date=None,
                            thumbnail_url=params.get("source_thumbnail_url"),
                        )
                        if not params.get("source_title"):
                            resolved = se.resolve_urls(
                                [url],
                                cookies_path=cookies,
                                cancel_event=job.cancel_event,
                                progress=log,
                            )
                            if resolved:
                                orig = resolved[0]
                        url = se.find_preferred_audio_url(
                            orig,
                            cookies_path=cookies,
                            cancel_event=job.cancel_event,
                            progress=log,
                            expected_artist=params.get("expected_artist"),
                            expected_title=params.get("expected_title"),
                            expected_duration_s=params.get("expected_duration_s"),
                            prefer_explicit=prefer_explicit,
                        )
            title_hint = str(params.get("source_title") or "").strip() or None
            uploader_hint = str(params.get("source_uploader") or "").strip() or None
            duration_hint = params.get("source_duration_s")
            if duration_hint is not None:
                try:
                    duration_hint = int(duration_hint)
                except (TypeError, ValueError):
                    duration_hint = None
            thumb_hint = params.get("source_thumbnail_url")
            if not isinstance(thumb_hint, str) or not thumb_hint.strip():
                thumb_hint = None

            with self._timed_stage(job, "download"):
                result = dl.download_music(
                    [url], params["output_dir"],
                    cookies_path=cookies,
                    verbose=verbose,
                    progress=log,
                    on_pct=on_progress,
                    cancel_event=job.cancel_event,
                    prefer_explicit=bool(params.get("allow_explicit", True)),
                    title_hint=title_hint,
                    uploader_hint=uploader_hint,
                    duration_hint=duration_hint,
                    thumbnail_hint=thumb_hint,
                    # Start bytes ASAP; iTunes naming/tags happen in postprocess.
                    defer_itunes=bool(title_hint),
                    reserve_output=lambda path: self._reserve_output(job, path),
                )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"
            elif result.output_paths:
                queued_at = time.monotonic()

                def finish_music() -> None:
                    job.timings["tag_queue"] = time.monotonic() - queued_at
                    self._finish_music(job, result, log)

                return finish_music

        elif job.kind == "embed_single":
            result = em.embed_single(
                Path(params["video"]), Path(params["thumb"]),
                Path(params["output_dir"]),
                progress=log,
                cancel_event=job.cancel_event,
            )
            if result.failed and not result.processed:
                job.state = FAILED
                job.error = "embed failed"

        elif job.kind == "embed_folder":
            result = em.embed_folder(
                Path(params["video_dir"]), Path(params["thumb_dir"]),
                Path(params["output_dir"]),
                progress=log,
                cancel_event=job.cancel_event,
            )
            if result.failed and not result.processed:
                job.state = FAILED
                job.error = "embed failed"

        elif job.kind in ("search", "search_more"):
            # Search itself runs through the queue so the GUI sees one
            # consistent status panel. `search_more` is the same call with a
            # larger limit; the listener slices off the already-shown prefix.
            from . import search as se
            if (
                job.kind == "search_more"
                and params.get("results_context") == "music"
            ):
                already = int(params.get("already_loaded", 0))
                limit = int(params.get("limit", 20))
                track_count_requested = max(0, limit - already)
                tracks, albums, albums_exhausted = se.search_youtube_more_music(
                    params["query"],
                    track_offset=already,
                    track_count=track_count_requested,
                    album_offset=int(params.get("album_offset", 0)),
                    album_fetch_count=int(params.get("album_fetch_count", 0)),
                    cookies_path=cookies,
                    cancel_event=job.cancel_event,
                    progress=log,
                    verbose=verbose,
                    videos_only=bool(params.get("videos_only", True)),
                    audio_only=bool(params.get("audio_only", False)),
                    use_youtube_music=bool(params.get("use_youtube_music", False)),
                )
                tracks_exhausted = (
                    track_count_requested > 0 and len(tracks) < track_count_requested
                )
                job.result = {
                    "tracks": tracks,
                    "albums": albums,
                    "albums_exhausted": albums_exhausted,
                    "tracks_exhausted": tracks_exhausted,
                }
            else:
                if job.kind == "search_more":
                    already = int(params.get("already_loaded", 0))
                    limit = int(params.get("limit", 20))
                    count = max(0, limit - already)
                    new_items, _, tracks_exhausted = se._search_youtube_more_regular(
                        params["query"],
                        track_offset=already,
                        track_count=count,
                        cookies_path=cookies,
                        cancel_event=job.cancel_event,
                        progress=log,
                        verbose=verbose,
                        videos_only=bool(params.get("videos_only", True)),
                        audio_only=bool(params.get("audio_only", False)),
                    )
                    job.result = {
                        "tracks": new_items,
                        "tracks_exhausted": tracks_exhausted,
                    }
                else:
                    stream_music = (
                        params.get("results_context") == "music"
                        and bool(params.get("use_youtube_music"))
                    )

                    def on_partial(phase: str, items: list) -> None:
                        if job.cancel_event.is_set():
                            raise _Cancelled()
                        job.result = {
                            "partial": True,
                            "phase": phase,
                            "items": items,
                        }
                        self._notify(job)

                    job.result = se.search_youtube(
                        params["query"],
                        limit=params.get("limit", 20),
                        cookies_path=cookies,
                        cancel_event=job.cancel_event,
                        progress=log,
                        videos_only=bool(params.get("videos_only", True)),
                        audio_only=bool(params.get("audio_only", False)),
                        use_youtube_music=bool(params.get("use_youtube_music", False)),
                        include_albums=bool(params.get("include_albums", False)),
                        include_playlists=bool(params.get("include_playlists", False)),
                        album_limit=int(params.get("album_limit") or 5),
                        verbose=verbose,
                        on_partial=on_partial if stream_music else None,
                    )

        elif job.kind == "resolve":
            from . import search as se
            results = se.resolve_urls(
                params["urls"],
                cookies_path=cookies,
                cancel_event=job.cancel_event,
                progress=log,
                verbose=verbose,
            )
            job.result = results

        elif job.kind == "source_resolve":
            from .sources import resolve as resolve_source
            tracks = resolve_source(
                params["platform"],
                params.get("urls") or [],
                text=params.get("text") or "",
                progress=log,
                cancel_event=job.cancel_event,
                cookies_path=cookies,
            )
            job.result = tracks

        elif job.kind == "source_match_all":
            with self._timed_stage(job, "matching"):
                self._match_tracks(job, log)

        else:
            raise ValueError(f"Unknown job kind: {job.kind}")

    def _match_tracks(self, job: Job, log: Callable[[str], None]) -> None:
        from . import search as se
        from .match_config import get_match_config
        from .sources.base import MATCH_PENDING, MusicTrack

        params = job.params
        tracks = [
            t if isinstance(t, MusicTrack) else MusicTrack.from_dict(t)
            for t in params.get("tracks") or []
        ]
        cfg = get_match_config(str(params.get("match_quality") or "balanced"))
        pending = deque(i for i, track in enumerate(tracks) if track.match_status == MATCH_PENDING)
        inflight: dict[Future, int] = {}
        next_start = 0.0
        messages: SimpleQueue[str] = SimpleQueue()
        stream = params.get("results_context") == "music"
        job.result = list(tracks)
        if stream:
            self._notify(job)

        def match(index: int):
            if job.cancel_event.is_set():
                raise _Cancelled()
            track = tracks[index]
            return se.find_youtube_match_for_track(
                track.artist, track.title, track.duration_s,
                cookies_path=params.get("cookies_path") or None,
                cancel_event=job.cancel_event,
                # Only the coordinator mutates job progress/result state.
                progress=messages.put,
                use_youtube_music=bool(params.get("use_youtube_music", False)),
                audio_only=bool(params.get("audio_only", True)),
                match_quality=cfg.name,
                prefer_explicit=bool(params.get("allow_explicit", True)),
            )

        try:
            while inflight or pending:
                if job.cancel_event.is_set():
                    raise _Cancelled()
                while True:
                    try:
                        log(messages.get_nowait())
                    except Empty:
                        break
                now = time.monotonic()
                if pending and len(inflight) < cfg.match_parallel and now >= next_start:
                    index = pending.popleft()
                    log(f"Matching {index + 1}/{len(tracks)} ({cfg.name}): {tracks[index].display_title(50)}")
                    with self._lock:
                        if self._closing:
                            raise _Cancelled()
                        future = self._match_executor.submit(match, index)
                    inflight[future] = index
                    # Pace starts rather than sleeping after each result;
                    # network time counts toward the existing delay.
                    next_start = time.monotonic() + cfg.inter_track_delay_s
                if not inflight:
                    job.cancel_event.wait(max(0.0, next_start - time.monotonic()))
                    continue
                completed, _ = wait(inflight, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = inflight.pop(future)
                    tracks[index] = tracks[index].with_match(future.result())
                    job.result = list(tracks)
                    if stream:
                        self._notify(job)
            while not messages.empty():
                log(messages.get_nowait())
            ok = sum(track.is_downloadable() for track in tracks)
            log(f"Matched {ok}/{len(tracks)} ({cfg.name})")
            job.result = tracks
        finally:
            for future in inflight:
                future.cancel()

    def _finish_music(self, job: Job, result: dl.MusicDownloadResult, log: Callable[[str], None]) -> None:
        params = job.params
        enrich = bool(params.get("enrich_metadata", True))
        lyrics = bool(params.get("download_lyrics", True))
        for i, path in enumerate(result.output_paths):
            info = (
                result.track_infos[i]
                if i < len(result.track_infos)
                else None
            )
            # Album downloads pass source_album — skip the downloader's
            # per-song iTunes guess so postprocess can match in album context.
            prefetch_match = (
                None
                if params.get("source_album")
                else (info.itunes_match if info else None)
            )
            track_info = mp.TrackInfo(
                title=info.title if info else "",
                uploader=info.uploader if info else "",
                parsed_artist=info.parsed_artist if info else "",
                parsed_title=info.parsed_title if info else "",
                duration_s=info.duration_s if info else None,
                thumbnail_url=info.thumbnail_url if info else None,
                itunes_match=prefetch_match,
                source_album=params.get("source_album") or "",
                source_album_artist=params.get("source_album_artist") or "",
                source_track_number=params.get("source_track_number"),
                source_disc_number=params.get("source_disc_number"),
                source_cover_url=params.get("source_cover_url"),
            )
            with self._timed_stage(job, "tagging"):
                pp = mp.process_track(
                    path,
                    track_info=track_info,
                    enrich_metadata=enrich,
                    download_lyrics=lyrics,
                    prefer_explicit=bool(params.get("allow_explicit", True)),
                    progress=log,
                    cancel_event=job.cancel_event,
                )
            if not pp.success:
                job.state = FAILED
                job.error = pp.message or "post-process failed"
                break
            if params.get("add_to_apple_music"):
                with self._timed_stage(job, "import"):
                    imp = am.import_to_library(
                        pp.final_path,
                        progress=log,
                        cancel_event=job.cancel_event,
                        remove_source=bool(params.get("apple_music_only")),
                        **({"playlist_id": params["apple_music_playlist_id"]} if params.get("apple_music_playlist_id") else {}),
                    )
                if not imp.success:
                    if params.get("apple_music_playlist_id"):
                        job.state = FAILED
                        job.error = f"Downloaded file saved, but playlist import failed: {imp.message}"
                        break
                    log(
                        f"WARN: Apple Music import failed"
                        f" — {imp.message}",
                    )

# ----------------------- cancellation sentinel ---------------------------- #

class _Cancelled(Exception):
    """Raised inside a worker callback to abort the running job."""


def _clamp_pct(pct: float) -> float:
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 100.0:
        return 100.0
    return v
