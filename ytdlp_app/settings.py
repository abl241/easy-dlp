"""Persistent key/value settings store.

Replaces the Java app's data.json. Stored in the user's home config dir so
it travels across reinstalls and doesn't pollute the project directory:

    macOS / Linux:  ~/.config/ytdlp-app/settings.json
    Windows:        %APPDATA%/ytdlp-app/settings.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


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

    "cookies_path": "",  # optional path to a Netscape-format cookies file
}


def _config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ytdlp-app"


class Settings:
    """Thread-safe JSON-backed settings store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_config_dir() / "settings.json")
        self._lock = Lock()
        self._data: dict[str, Any] = dict(_DEFAULTS)
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
            if isinstance(stored, dict):
                self._data.update({k: v for k, v in stored.items() if k in _DEFAULTS})
        except (OSError, json.JSONDecodeError):
            # corrupt file; fall back to defaults silently
            pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default if default is not None else _DEFAULTS.get(key, ""))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp.replace(self._path)
