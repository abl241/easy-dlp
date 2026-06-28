"""Standalone ffmpeg-based thumbnail embedding.

Mirrors the Java app's "Embed new thumbnail to video" feature. Operates on
*existing* audio/video files — independent of yt-dlp downloads.

Single-file mode:
    Provide an explicit video file and an explicit thumbnail image.

Folder mode:
    Provide a folder of thumbnails and a folder of audio files; for each
    thumbnail "<name>.{jpg,jpeg,png}" we look for "<name>.mp3" in the
    audio folder and embed the thumbnail into it.

Output is written to `out_dir` with the original audio filename. We refuse
to write into a path that resolves to the same file as the source, which
would corrupt the input.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runtime import find_ffmpeg


THUMB_EXTS = {".jpg", ".jpeg", ".png"}
ProgressFn = Callable[[str], None]
_FFMPEG_TIMEOUT_S = 10 * 60  # 10 minutes per file


@dataclass
class EmbedResult:
    processed: int
    failed: int
    output_dir: Path


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _run_ffmpeg(video: Path, thumb: Path, output: Path, progress: ProgressFn,
                ffmpeg_bin: Path) -> bool:
    if _same_path(video, output):
        progress(f"[refuse] output {output} is the same file as input — skipping")
        return False
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
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        progress(f"[ffmpeg] TIMEOUT after {_FFMPEG_TIMEOUT_S}s on {video.name}")
        return False
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        progress(f"[ffmpeg] FAILED ({proc.returncode}): {tail}")
        return False
    progress(f"[ffmpeg] wrote {output.name}")
    return True


def embed_single(
    video: Path | str,
    thumb: Path | str,
    out_dir: Path | str,
    progress: ProgressFn = lambda msg: None,
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
    ok = _run_ffmpeg(video, thumb, output, progress, ffmpeg_bin)
    return EmbedResult(processed=1 if ok else 0, failed=0 if ok else 1, output_dir=out_dir)


def embed_folder(
    video_dir: Path | str,
    thumb_dir: Path | str,
    out_dir: Path | str,
    progress: ProgressFn = lambda msg: None,
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
    thumbs = sorted(p for p in thumb_dir.iterdir() if p.suffix.lower() in THUMB_EXTS)
    for thumb in thumbs:
        audio = video_dir / f"{thumb.stem}.mp3"
        if not audio.is_file():
            progress(f"[skip] no audio match for {thumb.name}")
            continue
        output = out_dir / audio.name
        if _run_ffmpeg(audio, thumb, output, progress, ffmpeg_bin):
            processed += 1
        else:
            failed += 1
    if processed == 0 and failed == 0:
        progress("No matching <name>.mp3 / <name>.jpg pairs found.")
    return EmbedResult(processed=processed, failed=failed, output_dir=out_dir)
