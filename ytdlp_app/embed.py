"""Standalone ffmpeg-based thumbnail embedding.

Mirrors the Java app's "Embed new thumbnail to video" feature. Operates on
*existing* audio/video files — independent of yt-dlp downloads.

Single-file mode:
    Provide an explicit video file and an explicit thumbnail image.

Folder mode:
    Provide a folder of thumbnails and a folder of audio files; for each
    thumbnail "<name>.{jpg,jpeg,png}" we look for "<name>.mp3" in the
    audio folder and embed the thumbnail into it.

Output is written to `out_dir` with the original audio filename.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


THUMB_EXTS = {".jpg", ".jpeg", ".png"}
PROGRESS_FN = Callable[[str], None]


@dataclass
class EmbedResult:
    processed: int
    failed: int
    output_dir: Path


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run_ffmpeg(video: Path, thumb: Path, output: Path, progress: PROGRESS_FN) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # ffmpeg prints diagnostics on stderr; surface a short tail
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        progress(f"[ffmpeg] FAILED ({proc.returncode}): {tail}")
        return False
    progress(f"[ffmpeg] wrote {output.name}")
    return True


def embed_single(
    video: Path | str,
    thumb: Path | str,
    out_dir: Path | str,
    progress: PROGRESS_FN = lambda msg: None,
) -> EmbedResult:
    video, thumb, out_dir = Path(video), Path(thumb), Path(out_dir)
    if not _ffmpeg_available():
        progress("ffmpeg not found on PATH. Install it (brew install ffmpeg).")
        return EmbedResult(0, 1, out_dir)
    if not video.is_file():
        progress(f"Audio/video file not found: {video}")
        return EmbedResult(0, 1, out_dir)
    if not thumb.is_file():
        progress(f"Thumbnail file not found: {thumb}")
        return EmbedResult(0, 1, out_dir)
    output = out_dir / video.name
    ok = _run_ffmpeg(video, thumb, output, progress)
    return EmbedResult(processed=1 if ok else 0, failed=0 if ok else 1, output_dir=out_dir)


def embed_folder(
    video_dir: Path | str,
    thumb_dir: Path | str,
    out_dir: Path | str,
    progress: PROGRESS_FN = lambda msg: None,
) -> EmbedResult:
    video_dir, thumb_dir, out_dir = Path(video_dir), Path(thumb_dir), Path(out_dir)
    if not _ffmpeg_available():
        progress("ffmpeg not found on PATH. Install it (brew install ffmpeg).")
        return EmbedResult(0, 0, out_dir)
    if not thumb_dir.is_dir():
        progress(f"Thumbnail folder not found: {thumb_dir}")
        return EmbedResult(0, 0, out_dir)
    if not video_dir.is_dir():
        progress(f"Audio folder not found: {video_dir}")
        return EmbedResult(0, 0, out_dir)

    processed = failed = 0
    thumbs: Iterable[Path] = (p for p in thumb_dir.iterdir() if p.suffix.lower() in THUMB_EXTS)
    for thumb in thumbs:
        # match by basename: <name>.jpg -> <name>.mp3
        audio = video_dir / f"{thumb.stem}.mp3"
        if not audio.is_file():
            progress(f"[skip] no audio match for {thumb.name}")
            continue
        output = out_dir / audio.name
        if _run_ffmpeg(audio, thumb, output, progress):
            processed += 1
        else:
            failed += 1
    if processed == 0 and failed == 0:
        progress("No matching <name>.mp3 / <name>.jpg pairs found.")
    return EmbedResult(processed=processed, failed=failed, output_dir=out_dir)
