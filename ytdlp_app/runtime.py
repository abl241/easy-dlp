"""Runtime helpers: locating the ffmpeg binary across launch contexts.

A Python process launched from the shell inherits the user's PATH and finds
Homebrew binaries fine. A PyInstaller-built .app launched from Finder/Spotlight
gets a sanitized PATH (typically /usr/bin:/bin:/usr/sbin:/sbin) which does NOT
include /opt/homebrew/bin or /usr/local/bin. To make the app robust in both
cases we look in a few well-known locations after `which`.
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
from pathlib import Path


_COMMON_FFMPEG_PATHS = (
    "/opt/homebrew/bin/ffmpeg",       # macOS Apple Silicon Homebrew
    "/usr/local/bin/ffmpeg",          # macOS Intel Homebrew / generic /usr/local
    "/opt/local/bin/ffmpeg",          # MacPorts
    "/usr/bin/ffmpeg",                # Linux distros
    "/snap/bin/ffmpeg",               # Snap on Linux
    "C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    "C:/ffmpeg/bin/ffmpeg.exe",
)


def _bundled_ffmpeg() -> Path | None:
    """When packaged with PyInstaller, look for an ffmpeg binary bundled
    alongside the app. We check both ``sys._MEIPASS`` (one-file mode) and
    the executable's directory (folder mode)."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg")
        candidates.append(Path(meipass) / "ffmpeg.exe")
    exe_dir = Path(sys.executable).parent
    candidates.append(exe_dir / "ffmpeg")
    candidates.append(exe_dir / "ffmpeg.exe")
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> Path | None:
    """Return the absolute path to an ffmpeg executable, or None if not found.

    Search order:
        1. $FFMPEG_BINARY env var (escape hatch)
        2. PATH (`shutil.which`)
        3. Bundled with PyInstaller
        4. Common install locations
    """
    override = os.environ.get("FFMPEG_BINARY")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return Path(override)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path)

    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled

    for candidate in _COMMON_FFMPEG_PATHS:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return p

    return None


def ffmpeg_dir() -> str | None:
    """The directory containing ffmpeg, suitable for yt-dlp's `ffmpeg_location`.

    yt-dlp accepts either a binary path or a directory; passing the directory
    lets it find both ffmpeg and ffprobe if they're co-located.
    """
    binary = find_ffmpeg()
    return str(binary.parent) if binary else None
