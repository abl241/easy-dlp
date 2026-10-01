"""Optional Shazam recognition and related-song lookups, independent of tagging."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
from urllib.parse import urlparse, parse_qs


@dataclass(frozen=True)
class DiscoveredTrack:
    key: str
    title: str
    artist: str
    url: str
    apple_url: str = ""
    apple_id: str = ""
    preview_url: str = ""
    album: str = ""
    duration_s: float = 0
    genres: tuple[str, ...] = ()
    release_date: str = ""
    artwork_url: str = ""
    explicitness: str = ""
    label: str = ""
    track_number: int | None = None
    disc_number: int | None = None


def parse_track(raw: dict) -> DiscoveredTrack:
    apple_url = ""
    apple_id = ""
    preview_url = ""
    hub = raw.get("hub") or {}
    for action in hub.get("actions", []):
        uri = action.get("uri") or ""
        host = urlparse(uri).hostname or ""
        if urlparse(uri).scheme == "https" and (host.endswith(".itunes.apple.com") or host.endswith(".mzstatic.com")):
            preview_url = uri
        if action.get("type") == "applemusicplay":
            apple_id = str(action.get("id") or "")
    for option in hub.get("options", []):
        for action in option.get("actions", []):
            uri = action.get("uri") or ""
            parsed = urlparse(uri)
            if parsed.hostname == "music.apple.com" and parsed.path != "/subscribe":
                apple_url = uri
                apple_id = apple_id or parse_qs(parsed.query).get("i", [""])[0]
    metadata = {str(item.get('title', '')).casefold(): str(item.get('text', ''))
                for section in raw.get('sections', []) if section.get('type') == 'SONG'
                for item in section.get('metadata', [])}
    genre = (raw.get('genres') or {}).get('primary')
    images = raw.get('images') or {}
    return DiscoveredTrack(str(raw.get("key") or ""), raw.get("title") or "Unknown song",
                           raw.get("subtitle") or "Unknown artist", raw.get("url") or "",
                           apple_url, apple_id, preview_url,
                           album=metadata.get('album', ''), genres=(genre,) if genre else (),
                           release_date=metadata.get('released', ''),
                           label=metadata.get('label', ''),
                           artwork_url=images.get('coverart') or images.get('coverarthq') or '',
                           explicitness='explicit' if hub.get('explicit') is True else '')


def check_cancel(cancel):
    if cancel.is_set():
        raise RuntimeError("Cancelled")


async def _request(method, value):
    try:
        from shazamio import Shazam
    except ImportError as error:
        raise RuntimeError("ShazamIO is missing. Run ./run.sh --update, then restart the app.") from error
    client = Shazam()
    try:
        if method == "recognize":
            from .runtime import find_ffmpeg
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("FFmpeg is required to prepare audio for identification.")
            with tempfile.TemporaryDirectory(prefix="easy-dlp-fingerprint-") as folder:
                sample = str(Path(folder) / "sample.wav")
                process = await asyncio.create_subprocess_exec(
                    str(ffmpeg), "-nostdin", "-y", "-loglevel", "error",
                    "-i", str(value), "-t", "60", "-vn", "-ac", "1", "-ar", "16000", sample,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=30)
                    if process.returncode:
                        raise ValueError("Could not read this audio file. Try another supported audio format.")
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                return await asyncio.wait_for(client.recognize(sample), timeout=60)
        return await asyncio.wait_for(client.related_tracks(track_id=int(value), limit=20), timeout=30)
    finally:
        close = getattr(client, "close", None)
        if close:
            await close()


def identify(source, *, is_link, cookies, cancel):
    check_cancel(cancel)
    if not is_link:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError("Choose an existing audio file.")
        raw = asyncio.run(_request("recognize", str(path)))
    else:
        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be",
        ):
            raise ValueError("Paste a YouTube video link, or choose a local audio file.")
        import yt_dlp
        from .downloader import _shared_opts
        with tempfile.TemporaryDirectory(prefix="easy-dlp-identify-") as folder:
            opts = _shared_opts(folder, cookies)
            opts.update(format="bestaudio/best", outtmpl=str(Path(folder) / "audio.%(ext)s"),
                        ignoreerrors=False, noplaylist=True, noprogress=True,
                        socket_timeout=15, retries=1,
                        progress_hooks=[lambda _: check_cancel(cancel)])
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source, download=True)
                path = ydl.prepare_filename(info)
            check_cancel(cancel)
            raw = asyncio.run(_request("recognize", path))
    check_cancel(cancel)
    if not raw.get("track"):
        raise ValueError("No song recognized. Try a clearer sample or a different part of the song.")
    return enrich_tracks([parse_track(raw["track"])], cancel)[0]


def find_similar(key, cancel):
    check_cancel(cancel)
    if not key or not key.isdigit():
        raise ValueError("This result has no usable Shazam ID.")
    raw = asyncio.run(_request("related", key))
    check_cancel(cancel)
    result = []
    seen = {key}
    for item in raw.get("tracks", []):
        track = parse_track(item)
        if track.key and track.key not in seen:
            seen.add(track.key)
            result.append(track)
    return enrich_tracks(result, cancel)


def enrich_tracks(tracks, cancel):
    """Batch exact catalog IDs for fields omitted from Shazam similarities."""
    from .metadata.itunes import _fetch_results
    from urllib.parse import urlencode
    groups = {}
    for track in tracks:
        if track.apple_id.isdigit():
            parts = urlparse(track.apple_url).path.strip('/').split('/')
            country = parts[0].upper() if parts and len(parts[0]) == 2 else 'US'
            groups.setdefault(country, set()).add(track.apple_id)
    catalog = {}
    for country, ids in groups.items():
        check_cancel(cancel)
        rows = _fetch_results('https://itunes.apple.com/lookup?' + urlencode({'id': ','.join(sorted(ids)), 'entity': 'song', 'country': country}))
        catalog.update({str(row.get('trackId')): row for row in rows if row.get('kind') == 'song'})
    check_cancel(cancel)
    enriched = []
    for track in tracks:
        row = catalog.get(track.apple_id)
        if not row:
            enriched.append(track)
            continue
        genre = row.get('primaryGenreName')
        enriched.append(replace(track,
            album=row.get('collectionName') or track.album,
            duration_s=(row.get('trackTimeMillis') or 0) / 1000 or track.duration_s,
            genres=tuple(dict.fromkeys((*track.genres, *([genre] if genre else [])))),
            release_date=str(row.get('releaseDate') or track.release_date).split('T')[0],
            artwork_url=row.get('artworkUrl100') or track.artwork_url,
            explicitness=row.get('trackExplicitness') or track.explicitness,
            preview_url=track.preview_url or row.get('previewUrl', ''),
            track_number=row.get('trackNumber'), disc_number=row.get('discNumber')))
    return enriched
