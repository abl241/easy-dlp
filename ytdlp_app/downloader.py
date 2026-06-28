"""yt-dlp wrappers used by the GUI.

Each function accepts a `progress` text callback (for log lines) and an
`on_pct` numeric callback (for the per-job progress bar), plus an optional
`cancel_event`. The functions are blocking — call them from a worker thread
managed by `jobs.JobQueue`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yt_dlp

from .runtime import ffmpeg_dir


ProgressFn = Callable[[str], None]
PctFn = Callable[[float, str], None]

# yt-dlp's progress hook fires many times per second on fast connections.
# Throttle UI updates to avoid drowning the Tk event loop.
_MIN_PROGRESS_INTERVAL_S = 0.25


@dataclass
class DownloadResult:
    success: bool
    errors: int = 0
    message: str = ""


def _shared_opts(out_dir: str, cookies_path: str | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": str(Path(out_dir) / "%(title)s.%(ext)s"),
        "ignoreerrors": True,
        "retries": 3,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": False,
    }
    fd = ffmpeg_dir()
    if fd:
        opts["ffmpeg_location"] = fd
    if cookies_path and Path(cookies_path).is_file():
        opts["cookiefile"] = cookies_path
    return opts


class _PipeLogger:
    """Routes yt-dlp's logger output through a progress text callback."""

    def __init__(self, progress: ProgressFn) -> None:
        self._progress = progress

    def debug(self, msg: str) -> None:
        if msg and not msg.startswith("[debug] "):
            self._progress(msg)

    def info(self, msg: str) -> None:
        if msg:
            self._progress(msg)

    def warning(self, msg: str) -> None:
        self._progress(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        self._progress(f"ERROR: {msg}")


class _Cancelled(Exception):
    """Internal sentinel raised from progress hooks to abort yt-dlp."""


def _attach_progress(
    opts: dict[str, Any],
    progress: ProgressFn,
    on_pct: PctFn | None,
    cancel_event: threading.Event | None,
) -> None:
    last_emit = {"t": 0.0}

    def hook(d: dict[str, Any]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()

        status = d.get("status")
        if status == "downloading":
            # Always update the percentage (cheap), throttle the text.
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct_num = (downloaded / total * 100.0) if total else 0.0

            now = time.monotonic()
            send_text = (now - last_emit["t"]) >= _MIN_PROGRESS_INTERVAL_S
            if send_text:
                last_emit["t"] = now
                pct = (d.get("_percent_str") or "").strip() or f"{pct_num:.1f}%"
                speed = (d.get("_speed_str") or "").strip() or "?/s"
                eta = (d.get("_eta_str") or "").strip() or "?"
                filename = Path(d.get("filename") or "").name
                msg = f"[downloading] {pct} @ {speed}, ETA {eta} — {filename}"
                if on_pct is not None:
                    on_pct(pct_num, msg)
                else:
                    progress(msg)
            elif on_pct is not None:
                on_pct(pct_num, "")  # pct-only update, no log line
        elif status == "finished":
            last_emit["t"] = 0.0
            filename = Path(d.get("filename") or "").name
            msg = f"[done] {filename}"
            if on_pct is not None:
                on_pct(100.0, msg)
            else:
                progress(msg)
        elif status == "error":
            msg = f"[error] {d.get('filename') or ''}"
            if on_pct is not None:
                on_pct(0.0, msg)
            else:
                progress(msg)

    def pp_hook(d: dict[str, Any]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        ppname = d.get("postprocessor") or "postprocess"
        status = d.get("status")
        if status == "started":
            progress(f"[{ppname}] starting...")
        elif status == "finished":
            progress(f"[{ppname}] done")

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    opts["logger"] = _PipeLogger(progress)


def _run(
    urls: Iterable[str],
    opts: dict[str, Any],
    *,
    progress: ProgressFn,
    on_pct: PctFn | None,
    cancel_event: threading.Event | None,
) -> DownloadResult:
    _attach_progress(opts, progress, on_pct, cancel_event)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download(list(urls))
    except _Cancelled:
        return DownloadResult(success=False, message="cancelled")
    except yt_dlp.utils.DownloadError as e:
        return DownloadResult(success=False, message=str(e))
    except Exception as e:  # noqa: BLE001
        return DownloadResult(success=False, message=f"{type(e).__name__}: {e}")
    return DownloadResult(success=code == 0, errors=int(code or 0))


# ----------------------------- public API --------------------------------- #

def download_audio(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
    on_pct: PctFn | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Download audio as MP3 with embedded thumbnail + metadata."""
    opts = _shared_opts(out_dir, cookies_path)
    opts.update({
        "format": "bestaudio/best",
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
    })
    return _run(urls, opts, progress=progress, on_pct=on_pct, cancel_event=cancel_event)


def download_video(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
    on_pct: PctFn | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Download video as MP4 (H.264/AAC where possible) with embedded thumbnail.

    The format selector prefers MP4-compatible streams so we typically only
    remux (much faster) instead of re-encoding.
    """
    opts = _shared_opts(out_dir, cookies_path)
    opts.update({
        "format": "bv*[vcodec!^=vp9]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "embedthumbnail": True,
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
    })
    return _run(urls, opts, progress=progress, on_pct=on_pct, cancel_event=cancel_event)


def download_thumbnails_only(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
    on_pct: PctFn | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """Download just the thumbnail image (converted to JPG)."""
    opts = _shared_opts(out_dir, cookies_path)
    opts.update({
        "skip_download": True,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    })
    return _run(urls, opts, progress=progress, on_pct=on_pct, cancel_event=cancel_event)


def parse_urls(text: str) -> list[str]:
    """Split a user-entered string into URLs. One URL per line; whitespace-only
    lines are dropped. We deliberately do NOT split on commas — some valid
    URLs contain commas in query strings."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]
