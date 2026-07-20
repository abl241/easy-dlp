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
from typing import Any


def project_root() -> Path:
    """Repo root (parent of the ``ytdlp_app`` package)."""
    return Path(__file__).resolve().parent.parent


def app_icon_path() -> Path | None:
    """Return ``assets/icon.png`` if present."""
    candidate = project_root() / "assets" / "icon.png"
    return candidate if candidate.is_file() else None


def set_macos_dock_icon(icon_path: Path | None = None) -> bool:
    """Replace the Python rocket in the Dock with our app icon (macOS only)."""
    if sys.platform != "darwin":
        return False
    path = icon_path or app_icon_path()
    if path is None or not path.is_file():
        return False
    try:
        from AppKit import NSApplication, NSImage  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        NSApplication.sharedApplication()
        image = NSImage.alloc().initByReferencingFile_(str(path.resolve()))
        if image is None or not image.isValid():
            return False
        NSApplication.sharedApplication().setApplicationIconImage_(image)
        return True
    except Exception:
        return False


def apply_window_icon(root: Any) -> None:
    """Set the window/taskbar icon from ``assets/icon.png`` when available."""
    path = app_icon_path()
    if path is None:
        return
    set_macos_dock_icon(path)
    try:
        from PIL import Image, ImageTk

        img = Image.open(path).convert("RGBA")
        img = img.resize((256, 256), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        # Keep a reference on the widget so Tk doesn't garbage-collect it.
        root._easy_dlp_icon_photo = photo  # noqa: SLF001
        root.iconphoto(True, photo)
    except Exception:
        pass


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


_COMMON_NODE_PATHS = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/node",
)


@functools.lru_cache(maxsize=1)
def find_deno() -> Path | None:
    """Return a Deno binary for yt-dlp's YouTube JS challenge solver."""
    try:
        from deno import find_deno_bin

        candidate = Path(find_deno_bin())
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except (ImportError, FileNotFoundError, OSError):
        pass

    on_path = shutil.which("deno")
    if on_path:
        return Path(on_path)

    for candidate in _COMMON_NODE_PATHS:
        deno = candidate.replace("/node", "/deno")
        p = Path(deno)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


@functools.lru_cache(maxsize=1)
def find_node() -> Path | None:
    """Return a Node.js binary as a fallback JS runtime for yt-dlp."""
    on_path = shutil.which("node")
    if on_path:
        return Path(on_path)

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        versions = sorted(
            (p / "bin" / "node" for p in nvm_root.iterdir() if p.is_dir()),
            reverse=True,
        )
        for candidate in versions:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    for candidate in _COMMON_NODE_PATHS:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


@functools.lru_cache(maxsize=1)
def ytdlp_js_runtimes() -> dict[str, dict[str, str | None]]:
    """JS runtime config for yt-dlp (Deno preferred; Node as fallback)."""
    runtimes: dict[str, dict[str, str | None]] = {}
    deno = find_deno()
    if deno:
        runtimes["deno"] = {"path": str(deno)}
    node = find_node()
    if node:
        runtimes["node"] = {"path": str(node)}
    return runtimes


def apply_ytdlp_runtime_opts(opts: dict[str, Any]) -> None:
    """Attach ffmpeg and JS runtime settings needed for YouTube downloads."""
    from .rate_limit import apply_rate_limit_opts

    fd = ffmpeg_dir()
    if fd:
        opts["ffmpeg_location"] = fd
    runtimes = ytdlp_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    apply_rate_limit_opts(opts)
