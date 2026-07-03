"""Import tagged MP3s into the macOS Music app library."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[str], None]

_AUTO_ADD_PARTS = (
    "Music",
    "Music",
    "Media.localized",
    "Automatically Add to Music.localized",
)


@dataclass
class ImportResult:
    success: bool
    message: str = ""


def is_supported() -> bool:
    return sys.platform == "darwin"


def auto_add_folder() -> Path:
    return Path.home().joinpath(*_AUTO_ADD_PARTS)


def import_to_library(
    path: str | Path,
    *,
    progress: ProgressFn = lambda msg: None,
    cancel_event: threading.Event | None = None,
    remove_source: bool = False,
) -> ImportResult:
    """Add a tagged audio file to the Music app library."""
    if not is_supported():
        return ImportResult(success=False, message="macOS only")

    if cancel_event is not None and cancel_event.is_set():
        return ImportResult(success=False, message="cancelled")

    src = Path(path)
    if not src.is_file():
        return ImportResult(success=False, message=f"file not found: {path}")

    progress("[music] adding to Apple Music…")
    result = _import_via_applescript(src)
    if not result.success:
        progress(
            f"WARN: AppleScript import failed ({result.message})"
            " — trying auto-add folder",
        )
        result = _import_via_auto_add_folder(src, progress=progress)

    if result.success:
        progress("[music] added to Apple Music")
        if remove_source:
            _remove_source(src, progress=progress)
        return result

    return result


def _remove_source(path: Path, *, progress: ProgressFn) -> None:
    try:
        path.unlink()
        progress("[music] removed local copy after Apple Music import")
    except OSError as e:
        progress(f"WARN: could not remove local copy — {e}")


def _import_via_applescript(path: Path) -> ImportResult:
    resolved = str(path.resolve())
    escaped = resolved.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Music" to add POSIX file "{escaped}"'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ImportResult(success=False, message="timed out")
    except OSError as e:
        return ImportResult(success=False, message=str(e))

    if proc.returncode == 0:
        return ImportResult(success=True)

    err = (proc.stderr or proc.stdout or "osascript failed").strip()
    return ImportResult(success=False, message=err)


def _import_via_auto_add_folder(
    path: Path,
    *,
    progress: ProgressFn,
) -> ImportResult:
    folder = auto_add_folder()
    if not folder.is_dir():
        return ImportResult(
            success=False,
            message=f"auto-add folder not found: {folder}",
        )
    dest = folder / path.name
    try:
        shutil.copy2(path, dest)
    except OSError as e:
        return ImportResult(success=False, message=str(e))
    progress("[music] copied to Automatically Add to Music folder")
    return ImportResult(success=True)
