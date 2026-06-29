"""Synced/plain lyrics lookup via LRCLIB."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_USER_AGENT = "easy-dlp/2.1 (music lyrics)"
_GET_URL = "https://lrclib.net/api/get"
_SEARCH_URL = "https://lrclib.net/api/search"


@dataclass(frozen=True)
class Lyrics:
    plain: str = ""
    synced: str = ""


def fetch_lyrics(
    artist: str,
    title: str,
    *,
    album: str = "",
    duration_s: int | None = None,
) -> Lyrics | None:
    """Fetch lyrics from LRCLIB. Returns None when not found."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return None

    # Strict lookup — try without album first (album strings from iTunes are
    # often edition-specific and cause LRCLIB misses).
    attempts: list[dict[str, str]] = []
    base = {"artist_name": artist, "track_name": title}
    if duration_s is not None:
        attempts.append({**base, "duration": str(int(duration_s))})
    attempts.append(dict(base))

    # Album-qualified attempt last.
    if album:
        entry = dict(base)
        if duration_s is not None:
            entry["duration"] = str(int(duration_s))
        entry["album_name"] = album
        attempts.append(entry)

    for params in attempts:
        result = _get_lyrics(params)
        if result is not None:
            return result

    return _search_lyrics(artist, title, duration_s)


def _get_lyrics(params: dict[str, str]) -> Lyrics | None:
    url = f"{_GET_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return _payload_to_lyrics(payload)


def _search_lyrics(
    artist: str,
    title: str,
    duration_s: int | None,
) -> Lyrics | None:
    params = urllib.parse.urlencode({
        "track_name": title,
        "artist_name": artist,
    })
    url = f"{_SEARCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, urllib.error.HTTPError):
        return None

    if not isinstance(payload, list) or not payload:
        return None

    best: dict | None = None
    best_score = 0.0
    for row in payload:
        if not isinstance(row, dict):
            continue
        score = _score_search_row(row, artist, title, duration_s)
        if score > best_score:
            best_score = score
            best = row

    if best is None or best_score < 0.5:
        return None

    track_id = best.get("id")
    if isinstance(track_id, int):
        return _get_lyrics_by_id(track_id)
    return _payload_to_lyrics(best)


def _get_lyrics_by_id(track_id: int) -> Lyrics | None:
    url = f"https://lrclib.net/api/get/{track_id}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, urllib.error.HTTPError):
        return None
    return _payload_to_lyrics(payload)


def _payload_to_lyrics(payload: object) -> Lyrics | None:
    if not isinstance(payload, dict):
        return None
    plain = payload.get("plainLyrics") or ""
    synced = payload.get("syncedLyrics") or ""
    if not isinstance(plain, str):
        plain = ""
    if not isinstance(synced, str):
        synced = ""
    if not plain.strip() and not synced.strip():
        return None
    return Lyrics(plain=plain.strip(), synced=synced.strip())


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _score_search_row(
    row: dict,
    artist: str,
    title: str,
    duration_s: int | None,
) -> float:
    row_artist = str(row.get("artistName") or "")
    row_title = str(row.get("trackName") or "")
    a = set(_normalize(row_artist).split())
    b = set(_normalize(artist).split())
    t = set(_normalize(row_title).split())
    u = set(_normalize(title).split())
    artist_score = len(a & b) / len(a | b) if a and b else 0.0
    title_score = len(t & u) / len(t | u) if t and u else 0.0
    score = title_score * 0.6 + artist_score * 0.4

    row_dur = row.get("duration")
    if duration_s is not None and isinstance(row_dur, (int, float)):
        delta = abs(int(duration_s) - int(row_dur))
        if delta <= 2:
            score += 0.2
        elif delta > 10:
            score -= 0.15
    return score
