"""Standalone ffmpeg-based thumbnail embedding.

Mirrors the Java app's "Embed new thumbnail to video" feature. Operates on
*existing* audio/video files — independent of yt-dlp downloads.

Cancellation: we run ffmpeg via Popen and poll both the process and the
cancel event; on cancel we call `terminate()` and (after a grace period)
`kill()`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runtime import find_ffmpeg


THUMB_EXTS = {".jpg", ".jpeg", ".png"}
ProgressFn = Callable[[str], None]
_FFMPEG_TIMEOUT_S = 10 * 60        # hard wall-clock limit per file
_POLL_INTERVAL_S = 0.2             # how often to check the cancel flag
_KILL_GRACE_S = 2.0                # SIGTERM -> SIGKILL grace period


@dataclass
class EmbedResult:
    processed: int
    failed: int
    output_dir: Path
    cancelled: bool = False


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _run_ffmpeg(
    video: Path,
    thumb: Path,
    output: Path,
    progress: ProgressFn,
    ffmpeg_bin: Path,
    cancel_event: threading.Event | None,
) -> str:
    """Returns "ok", "cancelled", or "fail"."""
    if _same_path(video, output):
        progress(f"[refuse] output {output} is the same file as input — skipping")
        return "fail"
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_bin), "-y",
        "-i", str(video),
        "-i", str(thumb),
        "-acodec", "libmp3lame",
        "-b:a", "256k",
        "-c:v", "copy",
        "-map", "0:a:0",
        "-map", "1:v:0",
        str(output),
    ]
    progress(f"[ffmpeg] {video.name}  +  {thumb.name}  -->  {output}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        progress(f"[ffmpeg] FAILED: binary not found at {ffmpeg_bin}")
        return "fail"

    start = time.monotonic()
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if cancel_event is not None and cancel_event.is_set():
            progress("[ffmpeg] cancel requested; terminating...")
            proc.terminate()
            grace_until = time.monotonic() + _KILL_GRACE_S
            while proc.poll() is None and time.monotonic() < grace_until:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            # Clean up partial output.
            try:
                if output.is_file():
                    output.unlink()
            except OSError:
                pass
            return "cancelled"
        if time.monotonic() - start > _FFMPEG_TIMEOUT_S:
            progress(f"[ffmpeg] TIMEOUT after {_FFMPEG_TIMEOUT_S}s on {video.name}; terminating")
            proc.terminate()
            try:
                proc.wait(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return "fail"
        time.sleep(_POLL_INTERVAL_S)

    if rc != 0:
        # Drain remaining output for diagnostics.
        try:
            tail = (proc.stdout.read() or "").strip().splitlines()[-5:] if proc.stdout else []
        except Exception:  # noqa: BLE001
            tail = []
        progress(f"[ffmpeg] FAILED ({rc}): {' / '.join(tail)}")
        return "fail"

    progress(f"[ffmpeg] wrote {output.name}")
    return "ok"


def embed_single(
    video: Path | str,
    thumb: Path | str,
    out_dir: Path | str,
    *,
    progress: ProgressFn = lambda msg: None,
    cancel_event: threading.Event | None = None,
) -> EmbedResult:
    video, thumb, out_dir = Path(video), Path(thumb), Path(out_dir)
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        progress("ffmpeg not found. Install it (macOS: brew install ffmpeg) "
                 "or set FFMPEG_BINARY.")
        return EmbedResult(0, 1, out_dir)
    if not video.is_file():
        progress(f"Audio/video file not found: {video}")
        return EmbedResult(0, 1, out_dir)
    if not thumb.is_file():
        progress(f"Thumbnail file not found: {thumb}")
        return EmbedResult(0, 1, out_dir)
    if _same_path(out_dir, video.parent):
        progress(f"[refuse] output directory equals input directory — would "
                 f"overwrite {video.name}; pick a different output folder.")
        return EmbedResult(0, 1, out_dir)
    output = out_dir / video.name
    rc = _run_ffmpeg(video, thumb, output, progress, ffmpeg_bin, cancel_event)
    return EmbedResult(
        processed=1 if rc == "ok" else 0,
        failed=1 if rc == "fail" else 0,
        output_dir=out_dir,
        cancelled=(rc == "cancelled"),
    )


def embed_folder(
    video_dir: Path | str,
    thumb_dir: Path | str,
    out_dir: Path | str,
    *,
    progress: ProgressFn = lambda msg: None,
    cancel_event: threading.Event | None = None,
) -> EmbedResult:
    video_dir, thumb_dir, out_dir = Path(video_dir), Path(thumb_dir), Path(out_dir)
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        progress("ffmpeg not found. Install it (macOS: brew install ffmpeg) "
                 "or set FFMPEG_BINARY.")
        return EmbedResult(0, 0, out_dir)
    if not thumb_dir.is_dir():
        progress(f"Thumbnail folder not found: {thumb_dir}")
        return EmbedResult(0, 0, out_dir)
    if not video_dir.is_dir():
        progress(f"Audio folder not found: {video_dir}")
        return EmbedResult(0, 0, out_dir)
    if _same_path(out_dir, video_dir):
        progress("[refuse] output folder equals audio folder — would "
                 "overwrite source files. Pick a different output folder.")
        return EmbedResult(0, 0, out_dir)

    processed = failed = 0
    cancelled = False
    thumbs = sorted(p for p in thumb_dir.iterdir() if p.suffix.lower() in THUMB_EXTS)
    for thumb in thumbs:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        audio = video_dir / f"{thumb.stem}.mp3"
        if not audio.is_file():
            progress(f"[skip] no audio match for {thumb.name}")
            continue
        output = out_dir / audio.name
        rc = _run_ffmpeg(audio, thumb, output, progress, ffmpeg_bin, cancel_event)
        if rc == "ok":
            processed += 1
        elif rc == "cancelled":
            cancelled = True
            break
        else:
            failed += 1
    if processed == 0 and failed == 0 and not cancelled:
        progress("No matching <name>.mp3 / <name>.jpg pairs found.")
    return EmbedResult(processed=processed, failed=failed,
                       output_dir=out_dir, cancelled=cancelled)
