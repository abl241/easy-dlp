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

_library_cache: list[tuple[str, str]] | None = None
_library_cache_lock = threading.Lock()


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


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def track_exists_in_library(artist: str, title: str) -> bool:
    """True if a matching song is already in the Music app library."""
    if not is_supported():
        return False
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return False
    with _library_cache_lock:
        cache = _library_cache
    if cache is not None:
        return _track_in_cache(cache, artist, title)
    return _track_exists_in_library_applescript(artist, title)


def begin_library_cache(*, progress: ProgressFn = lambda msg: None) -> None:
    """Load Music library metadata once for batch duplicate checks."""
    global _library_cache
    if not is_supported():
        return
    with _library_cache_lock:
        if _library_cache is not None:
            return
    progress("[music] loading Apple Music library for duplicate check…")
    entries = _fetch_library_entries()
    with _library_cache_lock:
        _library_cache = entries
    progress(f"[music] library loaded ({len(entries)} tracks)")


def end_library_cache() -> None:
    global _library_cache
    with _library_cache_lock:
        _library_cache = None


def _fetch_library_entries() -> list[tuple[str, str]]:
    script = '''
tell application "Music"
  set outText to ""
  repeat with tr in (every track of library playlist 1)
    set outText to outText & (name of tr) & tab & (artist of tr) & linefeed
  end repeat
  return outText
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        title, artist = line.split("\t", 1)
        title = title.strip()
        artist = artist.strip()
        if title:
            entries.append((title, artist))
    return entries


def _track_in_cache(
    cache: list[tuple[str, str]],
    artist: str,
    title: str,
) -> bool:
    title_cf = title.casefold()
    artist_cf = artist.casefold() if artist else ""
    for lib_title, lib_artist in cache:
        if title_cf not in lib_title.casefold():
            continue
        if not artist_cf or artist_cf in lib_artist.casefold():
            return True
    return False


def _track_exists_in_library_applescript(artist: str, title: str) -> bool:
    esc_artist = _escape_applescript_string(artist)
    esc_title = _escape_applescript_string(title)
    script = f'''tell application "Music"
  set artistQ to "{esc_artist}"
  set titleQ to "{esc_title}"
  if artistQ is "" then
    set searchQ to titleQ
  else
    set searchQ to artistQ & " " & titleQ
  end if
  try
    set hits to (search library playlist 1 for searchQ only songs)
    repeat with tr in hits
      set trName to name of tr
      set trArtist to artist of tr
      if artistQ is "" then
        if trName contains titleQ then return "yes"
      else
        if (trName contains titleQ) and (trArtist contains artistQ) then return "yes"
      end if
    end repeat
  end try
  return "no"
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    return (proc.stdout or "").strip().lower() == "yes"


def _import_via_applescript(path: Path) -> ImportResult:
    resolved = str(path.resolve())
    escaped = _escape_applescript_string(resolved)
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
