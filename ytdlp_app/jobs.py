"""Concurrent job queue for downloads and embed tasks.

A `JobQueue` owns a bounded `ThreadPoolExecutor` plus an in-process pending
list. Callers `enqueue(...)` a `Job` describing one unit of work; the queue
finds a free worker (or queues the job until one frees up) and runs the
appropriate function from `downloader` / `embed`. Job state mutations are
delivered to a single listener callback which is responsible for marshaling
them to the Tk main thread.
"""

from __future__ import annotations

import itertools
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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
    def __init__(self, max_parallel: int, listener: Listener) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._listener = listener
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_parallel,
            thread_name_prefix="ytdlp-job",
        )
        self._id_seq = itertools.count(1)
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._futures: dict[int, Future] = {}

    # --------- pool configuration --------- #

    def set_max_parallel(self, n: int) -> None:
        """Change the worker count. The old executor finishes its current
        jobs in the background; new jobs go to the new executor."""
        n = max(1, int(n))
        if n == self._max_parallel:
            return
        old = self._executor
        self._max_parallel = n
        self._executor = ThreadPoolExecutor(
            max_workers=n, thread_name_prefix="ytdlp-job",
        )
        # Don't wait on old jobs here — they keep running on the old pool.
        old.shutdown(wait=False)

    # --------- enqueue + cancel --------- #

    def enqueue(self, kind: str, label: str, **params: Any) -> Job:
        with self._lock:
            job = Job(
                id=next(self._id_seq),
                kind=kind,
                label=label,
                params=params,
            )
            self._jobs[job.id] = job
        self._notify(job)
        # submit() may run immediately on a free worker thread.
        fut = self._executor.submit(self._run_job, job)
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
        self.cancel_all()
        self._executor.shutdown(wait=wait)

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

    def _run_job(self, job: Job) -> None:
        if job.cancel_event.is_set():
            job.state = CANCELLED
            self._notify(job)
            return

        job.state = RUNNING
        job.progress_msg = "Starting..."
        self._notify(job)

        try:
            self._dispatch(job)
        except _Cancelled:
            job.state = CANCELLED
            job.progress_msg = "Cancelled."
        except Exception as e:  # noqa: BLE001
            job.state = FAILED
            job.error = f"{type(e).__name__}: {e}"
            job.progress_msg = job.error
        else:
            if job.cancel_event.is_set():
                job.state = CANCELLED
                job.progress_msg = "Cancelled."
            elif job.state != FAILED:
                job.state = DONE
                if not job.progress_msg or job.progress_msg.startswith("Starting"):
                    job.progress_msg = "Done."
        finally:
            self._notify(job)

    def _dispatch(self, job: Job) -> None:
        """Translate Job.kind into a concrete function call."""
        def on_progress(pct: float, msg: str) -> None:
            if job.cancel_event.is_set():
                raise _Cancelled()
            job.progress_pct = _clamp_pct(pct)
            if msg:
                job.progress_msg = msg
            self._notify(job)

        def log(msg: str) -> None:
            on_progress(job.progress_pct, msg)

        params = job.params
        cookies = params.get("cookies_path") or None

        if job.kind == "audio":
            result = dl.download_audio(
                [params["url"]], params["output_dir"],
                cookies_path=cookies,
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
                progress=log,
                on_pct=on_progress,
                cancel_event=job.cancel_event,
            )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"

        elif job.kind == "music":
            result = dl.download_music(
                [params["url"]], params["output_dir"],
                cookies_path=cookies,
                progress=log,
                on_pct=on_progress,
                cancel_event=job.cancel_event,
            )
            if not result.success:
                job.state = FAILED
                job.error = result.message or "see log"
            elif result.output_paths:
                enrich = bool(params.get("enrich_metadata", True))
                lyrics = bool(params.get("download_lyrics", True))
                for i, path in enumerate(result.output_paths):
                    info = (
                        result.track_infos[i]
                        if i < len(result.track_infos)
                        else None
                    )
                    track_info = mp.TrackInfo(
                        title=info.title if info else "",
                        uploader=info.uploader if info else "",
                        parsed_artist=info.parsed_artist if info else "",
                        parsed_title=info.parsed_title if info else "",
                        duration_s=info.duration_s if info else None,
                        thumbnail_url=info.thumbnail_url if info else None,
                        itunes_match=info.itunes_match if info else None,
                    )
                    pp = mp.process_track(
                        path,
                        track_info=track_info,
                        enrich_metadata=enrich,
                        download_lyrics=lyrics,
                        progress=log,
                        cancel_event=job.cancel_event,
                    )
                    if not pp.success:
                        job.state = FAILED
                        job.error = pp.message or "post-process failed"
                        break

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
            results = se.search_youtube(
                params["query"],
                limit=params.get("limit", 20),
                cookies_path=cookies,
                cancel_event=job.cancel_event,
                progress=log,
                videos_only=bool(params.get("videos_only", True)),
                audio_only=bool(params.get("audio_only", False)),
            )
            job.result = results

        elif job.kind == "resolve":
            from . import search as se
            results = se.resolve_urls(
                params["urls"],
                cookies_path=cookies,
                cancel_event=job.cancel_event,
                progress=log,
            )
            job.result = results

        else:
            raise ValueError(f"Unknown job kind: {job.kind}")


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
