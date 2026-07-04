"""Music source registry — YouTube, Spotify, and future platforms."""

from __future__ import annotations

import threading

from .base import PLATFORM_CONFIGS, MusicTrack, PlatformConfig, ProgressFn
from .spotify import SpotifySource
from .youtube import YouTubeSource

_SOURCES = {
    "youtube": YouTubeSource(),
    "spotify": SpotifySource(),
}


def platform_ids() -> list[str]:
    return list(_SOURCES.keys())


def platform_labels() -> list[str]:
    return [PLATFORM_CONFIGS[pid].label for pid in platform_ids()]


def platform_config(platform_id: str) -> PlatformConfig:
    return PLATFORM_CONFIGS[platform_id]


def get_source(platform_id: str):
    try:
        return _SOURCES[platform_id]
    except KeyError as e:
        raise ValueError(f"Unknown platform: {platform_id}") from e


def detect_platform(url: str) -> str | None:
    for pid, source in _SOURCES.items():
        if source.is_url(url):
            return pid
    return None


def resolve(
    platform_id: str,
    urls: list[str],
    *,
    text: str = "",
    progress: ProgressFn = lambda msg: None,
    cancel_event: threading.Event | None = None,
    cookies_path: str | None = None,
) -> list[MusicTrack]:
    source = get_source(platform_id)
    tracks: list[MusicTrack] = []

    if urls:
        if platform_id == "youtube":
            tracks = source.resolve_urls(
                urls,
                progress=progress,
                cancel_event=cancel_event,
                cookies_path=cookies_path,
            )
        else:
            tracks = source.resolve_urls(
                urls,
                progress=progress,
                cancel_event=cancel_event,
            )

    if not tracks and text.strip():
        tracks = source.parse_text(text)

    return tracks


__all__ = [
    "MusicTrack",
    "PlatformConfig",
    "PLATFORM_CONFIGS",
    "detect_platform",
    "get_source",
    "platform_config",
    "platform_ids",
    "platform_labels",
    "resolve",
]
