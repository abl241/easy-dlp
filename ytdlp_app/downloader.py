"""yt-dlp wrappers used by the GUI.

Each function accepts a `progress` callable that receives status strings
to be shown in the UI. The functions are blocking — call them from a
worker thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yt_dlp

from .runtime import ffmpeg_dir


ProgressFn = Callable[[str], None]

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
    """Routes yt-dlp's logger output through a progress callback."""

    def __init__(self, progress: ProgressFn) -> None:
        self._progress = progress

    def debug(self, msg: str) -> None:
        # yt-dlp routes most info-level messages to debug; ignore the
        # noisy internal "[debug] ..." trace.
        if msg and not msg.startswith("[debug] "):
            self._progress(msg)

    def info(self, msg: str) -> None:
        if msg:
            self._progress(msg)

    def warning(self, msg: str) -> None:
        self._progress(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        self._progress(f"ERROR: {msg}")


def _attach_progress(opts: dict[str, Any], progress: ProgressFn) -> None:
    last_emit = {"t": 0.0}  # nonlocal mutability without `nonlocal`

    def hook(d: dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_emit["t"] < _MIN_PROGRESS_INTERVAL_S:
                return
            last_emit["t"] = now
            pct = (d.get("_percent_str") or "").strip() or "?%"
            speed = (d.get("_speed_str") or "").strip() or "?/s"
            eta = (d.get("_eta_str") or "").strip() or "?"
            filename = Path(d.get("filename") or "").name
            progress(f"[downloading] {pct} @ {speed}, ETA {eta} — {filename}")
        elif status == "finished":
            last_emit["t"] = 0.0  # allow next file's first tick through immediately
            filename = Path(d.get("filename") or "").name
            progress(f"[done] {filename}")
        elif status == "error":
            progress(f"[error] {d.get('filename') or ''}")

    def pp_hook(d: dict[str, Any]) -> None:
        ppname = d.get("postprocessor") or "postprocess"
        status = d.get("status")
        if status == "started":
            progress(f"[{ppname}] starting...")
        elif status == "finished":
            progress(f"[{ppname}] done")

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    opts["logger"] = _PipeLogger(progress)


def _run(urls: Iterable[str], opts: dict[str, Any], progress: ProgressFn) -> DownloadResult:
    _attach_progress(opts, progress)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download(list(urls))
    except yt_dlp.utils.DownloadError as e:
        return DownloadResult(success=False, message=str(e))
    except Exception as e:  # noqa: BLE001 — surface everything to the user
        return DownloadResult(success=False, message=f"{type(e).__name__}: {e}")
    return DownloadResult(success=code == 0, errors=int(code or 0))


def download_audio(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
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
    return _run(urls, opts, progress)


def download_video(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
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
    return _run(urls, opts, progress)


def download_thumbnails_only(
    urls: Iterable[str],
    out_dir: str,
    *,
    cookies_path: str | None = None,
    progress: ProgressFn = lambda msg: None,
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
    return _run(urls, opts, progress)


def parse_urls(text: str) -> list[str]:
    """Split a user-entered string into URLs.

    Accepts one URL per line. We keep whitespace-only lines out and trim each
    URL. We deliberately do NOT split on commas — some legitimate URLs contain
    commas in query strings, and "one URL per line" is enough of a convention.
    """
    result: list[str] = []
    for line in text.splitlines():
        tok = line.strip()
        if tok:
            result.append(tok)
    return result
