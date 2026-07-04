"""Spotify playlist / album / track resolution via spotifyscraper."""

from __future__ import annotations

import re
import threading

from .base import MATCH_PENDING, MusicTrack, ProgressFn, parse_track_list_text


_SPOTIFY_HOST_RE = re.compile(
    r"https?://open\.spotify\.com/(?:playlist|album|track)/[A-Za-z0-9]+",
    re.IGNORECASE,
)


class SpotifyResolveError(Exception):
    pass


class SpotifySource:
    id = "spotify"
    label = "Spotify"

    def is_url(self, url: str) -> bool:
        return bool(_SPOTIFY_HOST_RE.search(url or ""))

    def resolve_urls(
        self,
        urls: list[str],
        *,
        progress: ProgressFn = lambda msg: None,
        cancel_event: threading.Event | None = None,
    ) -> list[MusicTrack]:
        try:
            from spotify_scraper import SpotifyClient
        except ImportError as e:
            raise SpotifyResolveError(
                "spotifyscraper is not installed. Run: pip install spotifyscraper"
            ) from e

        out: list[MusicTrack] = []
        with SpotifyClient() as client:
            for raw in urls:
                url = raw.strip()
                if not url:
                    continue
                if cancel_event is not None and cancel_event.is_set():
                    break
                progress(f"Resolving Spotify: {url}")
                try:
                    out.extend(self._resolve_one(client, url, progress=progress))
                except Exception as e:
                    raise SpotifyResolveError(f"Failed to resolve {url}: {e}") from e
        return out

    def parse_text(self, text: str) -> list[MusicTrack]:
        return parse_track_list_text(text, source="text")

    def _resolve_one(self, client, url: str, *, progress: ProgressFn) -> list[MusicTrack]:
        kind = _spotify_kind(url)
        if kind == "playlist":
            progress("Fetching Spotify playlist...")
            playlist = client.get_playlist(url)
            return [_playlist_track_to_music_track(pt) for pt in playlist.tracks]
        if kind == "album":
            progress("Fetching Spotify album...")
            album = client.get_album(url)
            return [
                _album_track_to_music_track(t, album.name, index=i)
                for i, t in enumerate(album.tracks, start=1)
            ]
        if kind == "track":
            progress("Fetching Spotify track...")
            track = client.get_track(url)
            return [_track_to_music_track(track)]
        raise SpotifyResolveError(f"Unsupported Spotify URL: {url}")


def _spotify_kind(url: str) -> str:
    m = re.search(r"open\.spotify\.com/(playlist|album|track)/", url, re.I)
    if not m:
        raise SpotifyResolveError(f"Not a Spotify URL: {url}")
    return m.group(1).lower()


def _artist_names(artists) -> str:
    names = [a.name for a in artists if getattr(a, "name", None)]
    return ", ".join(names)


def _cover_url(images) -> str | None:
    if not images:
        return None
    best = max(images, key=lambda i: getattr(i, "width", 0) or 0)
    return getattr(best, "url", None)


def _track_to_music_track(
    track,
    *,
    album_name: str = "",
    fallback_track_number: int | None = None,
) -> MusicTrack:
    album = album_name or (track.album.name if getattr(track, "album", None) else "")
    duration_s = None
    if getattr(track, "duration_ms", None):
        duration_s = int(track.duration_ms / 1000)
    track_id = getattr(track, "id", "") or ""
    source_url = f"https://open.spotify.com/track/{track_id}" if track_id else None
    track_number = getattr(track, "track_number", None)
    if not isinstance(track_number, int):
        track_number = fallback_track_number
    disc_number = getattr(track, "disc_number", None)
    if not isinstance(disc_number, int):
        disc_number = None
    return MusicTrack(
        artist=_artist_names(track.artists),
        title=track.name,
        duration_s=duration_s,
        album=album,
        cover_url=_cover_url(getattr(track, "images", None)),
        thumbnail_url=_cover_url(getattr(track, "images", None)),
        track_number=track_number,
        disc_number=disc_number,
        source="spotify",
        source_url=source_url,
        match_status=MATCH_PENDING,
    )


def _playlist_track_to_music_track(entry) -> MusicTrack:
    track = entry.track
    album_name = track.album.name if getattr(track, "album", None) else ""
    return _track_to_music_track(track, album_name=album_name)


def _album_track_to_music_track(
    track, album_name: str, *, index: int | None = None,
) -> MusicTrack:
    return _track_to_music_track(
        track, album_name=album_name, fallback_track_number=index,
    )
