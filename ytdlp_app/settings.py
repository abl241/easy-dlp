"""Persistent key/value settings store.

Replaces the Java app's data.json. Stored in the user's OS-appropriate config
directory so it travels across reinstalls and doesn't pollute the project tree:

    macOS:    ~/Library/Application Support/ytdlp-app/settings.json
    Linux:    $XDG_CONFIG_HOME/ytdlp-app/settings.json (default ~/.config/...)
    Windows:  %APPDATA%/ytdlp-app/settings.json
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from threading import Lock
from typing import Any


_APP_NAME = "ytdlp-app"

_DEFAULTS: dict[str, Any] = {
    "audio_url": "",
    "audio_dir": str(Path.home() / "Music"),

    "video_url": "",
    "video_dir": str(Path.home() / "Movies"),

    "thumb_url": "",
    "thumb_dir": str(Path.home() / "Pictures"),

    "embed_video_dir": "",
    "embed_thumb_dir": "",
    "embed_out_dir": "",
    "embed_mode": "folder",  # "folder" or "single"

    "cookies_path": "",  # optional Netscape-format cookies file
}


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / _APP_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / _APP_NAME


class Settings:
    """Thread-safe JSON-backed settings store.

    The in-memory dict and disk writes are guarded by a lock, but the lock is
    released before the actual write() to avoid blocking other threads on disk
    I/O — we serialize the snapshot to a string under the lock, then flush
    outside it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_config_dir() / "settings.json")
        self._lock = Lock()
        self._data: dict[str, Any] = copy.deepcopy(_DEFAULTS)
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, json.JSONDecodeError):
            return  # corrupt or unreadable; keep defaults
        if not isinstance(stored, dict):
            return
        with self._lock:
            for k, v in stored.items():
                if k in _DEFAULTS:
                    self._data[k] = v

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
        if default is not None:
            return default
        return _DEFAULTS.get(key, "")

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            snapshot = json.dumps(self._data, indent=2)
        self._flush(snapshot)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            snapshot = json.dumps(self._data, indent=2)
        self._flush(snapshot)

    def _flush(self, snapshot: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(snapshot)
            tmp.replace(self._path)
        except OSError:
            # Best-effort: don't crash the UI if disk is full / read-only.
            pass
