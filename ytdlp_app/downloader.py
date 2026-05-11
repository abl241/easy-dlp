"""yt-dlp wrappers used by the GUI.

Each function accepts a `progress` callable that receives status strings
to be shown in the UI. The functions are blocking — call them from a
worker thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yt_dlp


ProgressFn = Callable[[str], None]


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
        # We always provide a quiet logger and feed status via progress_hooks.
        "quiet": True,
        "no_warnings": False,
    }
    if cookies_path and Path(cookies_path).is_file():
        opts["cookiefile"] = cookies_path
    return opts


def _attach_progress(opts: dict[str, Any], progress: ProgressFn) -> None:
    def hook(d: dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip() or "?%"
            speed = d.get("_speed_str", "").strip() or "?/s"
            eta = d.get("_eta_str", "").strip() or "?"
            filename = Path(d.get("filename") or d.get("info_dict", {}).get("title") or "").name
            progress(f"[downloading] {pct} @ {speed}, ETA {eta} — {filename}")
        elif status == "finished":
            filename = Path(d.get("filename") or "").name
            progress(f"[done] {filename}")
        elif status == "error":
            progress(f"[error] {d.get('filename') or ''}")

    def pp_hook(d: dict[str, Any]) -> None:
        status = d.get("status")
        ppname = d.get("postprocessor") or "postprocess"
        if status == "started":
            progress(f"[{ppname}] starting...")
        elif status == "finished":
            progress(f"[{ppname}] done")

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]

    class _PipeLogger:
        def debug(self, msg: str) -> None:
            if msg and not msg.startswith("[debug] "):
                progress(msg)

        def info(self, msg: str) -> None:
            if msg:
                progress(msg)

        def warning(self, msg: str) -> None:
            progress(f"WARN: {msg}")

        def error(self, msg: str) -> None:
            progress(f"ERROR: {msg}")

    opts["logger"] = _PipeLogger()


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
    """Download video as MP4 (H.264/AAC) with embedded thumbnail + metadata."""
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
        # NOTE: we no longer force re-encoding to libx264; the format selector
        # already prefers MP4-compatible streams, so most downloads complete
        # via remux only (much faster). Re-add postprocessor_args if you want
        # to force transcoding.
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

    Accepts one URL per line OR whitespace/comma-separated.
    """
    tokens = []
    for line in text.splitlines():
        for tok in line.replace(",", " ").split():
            tok = tok.strip()
            if tok:
                tokens.append(tok)
    return tokens
