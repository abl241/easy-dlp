"""iTunes Search API client for music metadata and cover art."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_USER_AGENT = "easy-dlp/2.1 (music metadata)"
_SEARCH_URL = "https://itunes.apple.com/search"
_LOOKUP_URL = "https://itunes.apple.com/lookup"

# Title tags that usually mean a different recording than the studio track.
_BAD_VERSION_RE = re.compile(
    r"\b(live|session|acoustic|karaoke|remix|mixed|dj mix|cover|instrumental)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ITunesTrack:
    artist: str
    title: str
    album: str
    year: int | None
    genre: str | None
    track_number: int | None
    disc_number: int | None
    duration_ms: int | None
    artwork_url: str | None


def search_track(
    artist: str,
    title: str,
    *,
    duration_s: int | None = None,
    limit: int = 25,
) -> ITunesTrack | None:
    """Return the best iTunes catalog match for a track, or None."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return None

    # When we know the artist, search their iTunes catalog first. A plain
    # song search often ranks unrelated same-title tracks higher (e.g.
    # Westend & Acraze "Apple Cider" vs beabadoobee "Apple Cider").
    if artist:
        artist_id = _lookup_artist_id(artist)
        if artist_id is not None:
            match = _search_artist_catalog(artist_id, title, duration_s)
            if match is not None:
                return match

    return _search_songs(artist, title, duration_s=duration_s, limit=limit)


def fetch_artwork(url: str | None, *, size: int = 600) -> bytes | None:
    """Download cover art; upscale iTunes thumb URLs when possible."""
    if not url:
        return None

    candidates = _artwork_url_candidates(url, size)
    for art_url in candidates:
        req = urllib.request.Request(art_url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
        except (OSError, urllib.error.HTTPError):
            continue
        if len(data) > 1000:
            return data
    return None


def _lookup_artist_id(artist: str) -> int | None:
    """Resolve an artist name to an iTunes artist ID."""
    params = urllib.parse.urlencode({
        "term": artist,
        "entity": "musicArtist",
        "limit": "8",
    })
    rows = _fetch_results(f"{_SEARCH_URL}?{params}")
    if not rows:
        return None

    best_id: int | None = None
    best_score = 0.0
    for row in rows:
        if row.get("wrapperType") != "artist":
            continue
        name = row.get("artistName")
        artist_id = row.get("artistId")
        if not isinstance(name, str) or not isinstance(artist_id, int):
            continue
        score = _artist_similarity(name, artist)
        if score > best_score:
            best_score = score
            best_id = artist_id

    if best_score < 0.55:
        return None
    return best_id


def _search_artist_catalog(
    artist_id: int,
    title: str,
    duration_s: int | None,
) -> ITunesTrack | None:
    """Find a song within a known artist's iTunes catalog."""
    params = urllib.parse.urlencode({
        "id": str(artist_id),
        "entity": "song",
        "limit": "200",
    })
    rows = _fetch_results(f"{_LOOKUP_URL}?{params}")
    if not rows:
        return None

    best: ITunesTrack | None = None
    best_score = 0.0
    for row in rows:
        if not isinstance(row, dict) or row.get("wrapperType") != "track":
            continue
        candidate = _row_to_track(row)
        if candidate is None:
            continue
        score = _score_title_match(candidate, title, duration_s)
        if score > best_score:
            best_score = score
            best = candidate

    if best_score < 0.50:
        return None
    return best


def _search_songs(
    artist: str,
    title: str,
    *,
    duration_s: int | None,
    limit: int,
) -> ITunesTrack | None:
    """Fallback: broad song search with strict artist matching."""
    term = " ".join(x for x in (artist, title) if x)
    params = urllib.parse.urlencode({
        "term": term,
        "entity": "song",
        "limit": str(max(1, limit)),
    })
    rows = _fetch_results(f"{_SEARCH_URL}?{params}")
    if not rows:
        return None

    best: ITunesTrack | None = None
    best_score = 0.0
    for row in rows:
        if not isinstance(row, dict) or row.get("wrapperType") != "track":
            continue
        candidate = _row_to_track(row)
        if candidate is None:
            continue
        score = _score_match(candidate, artist, title, duration_s)
        if score > best_score:
            best_score = score
            best = candidate

    if best_score < 0.55:
        return None
    # When artist is known, reject matches with poor artist overlap even if
    # the combined score cleared the threshold.
    if artist and best is not None:
        if _artist_similarity(best.artist, artist) < 0.35:
            return None
    return best


def _fetch_results(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)]


def _artwork_url_candidates(url: str, size: int) -> list[str]:
    """Build a list of artwork URLs to try, highest resolution first."""
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    for pattern, repl in (
        (r"100x100bb", f"{size}x{size}bb"),
        (r"100x100", f"{size}x{size}"),
        (r"60x60bb", f"{size}x{size}bb"),
        (r"60x60", f"{size}x{size}"),
    ):
        if pattern in url:
            add(re.sub(pattern, repl, url))

    add(url)
    return out


def _row_to_track(row: dict) -> ITunesTrack | None:
    title = row.get("trackName")
    artist = row.get("artistName")
    if not isinstance(title, str) or not isinstance(artist, str):
        return None
    album = row.get("collectionName")
    release = row.get("releaseDate")
    year = None
    if isinstance(release, str) and len(release) >= 4:
        try:
            year = int(release[:4])
        except ValueError:
            year = None
    duration_ms = row.get("trackTimeMillis")
    duration_ms = int(duration_ms) if isinstance(duration_ms, (int, float)) else None
    track_number = row.get("trackNumber")
    track_number = int(track_number) if isinstance(track_number, (int, float)) else None
    disc_number = row.get("discNumber")
    disc_number = int(disc_number) if isinstance(disc_number, (int, float)) else None
    genre = row.get("primaryGenreName")
    artwork = row.get("artworkUrl100") or row.get("artworkUrl60")
    return ITunesTrack(
        artist=artist,
        title=title,
        album=str(album) if isinstance(album, str) else "",
        year=year,
        genre=str(genre) if isinstance(genre, str) else None,
        track_number=track_number,
        disc_number=disc_number,
        duration_ms=duration_ms,
        artwork_url=str(artwork) if isinstance(artwork, str) else None,
    )


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _token_overlap(a: str, b: str) -> float:
    ta = set(_normalize(a).split())
    tb = set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _artist_similarity(candidate_artist: str, query_artist: str) -> float:
    """Score how closely two artist names match."""
    if not query_artist:
        return 0.0
    a = _normalize(query_artist)
    b = _normalize(candidate_artist)
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    # "Acraze & Westend" vs "Westend & Acraze" — compare as token sets.
    qa = set(a.split())
    cb = set(b.split())
    if qa and qa <= cb:
        return 0.85
    if cb and cb <= qa:
        return 0.85
    return _token_overlap(candidate_artist, query_artist)


def _duration_bonus(duration_s: int | None, duration_ms: int | None) -> float:
    if not duration_s or not duration_ms:
        return 0.0
    delta = abs(duration_s - duration_ms // 1000)
    if delta <= 2:
        return 0.20
    if delta <= 5:
        return 0.10
    if delta > 15:
        return -0.25
    return 0.0


def _version_penalty(title: str) -> float:
    if _BAD_VERSION_RE.search(title):
        return 0.20
    return 0.0


def _score_title_match(
    candidate: ITunesTrack,
    title: str,
    duration_s: int | None,
) -> float:
    """Score a candidate when the artist is already confirmed."""
    title_score = _token_overlap(candidate.title, title)
    if title_score < 0.5:
        return 0.0
    score = title_score * 0.70 + _duration_bonus(duration_s, candidate.duration_ms)
    score -= _version_penalty(candidate.title)
    return max(0.0, min(1.0, score))


def _score_match(
    candidate: ITunesTrack,
    artist: str,
    title: str,
    duration_s: int | None,
) -> float:
    title_score = _token_overlap(candidate.title, title)
    artist_score = _artist_similarity(candidate.artist, artist) if artist else 0.0

    if artist:
        # Artist match is the primary signal when we know who we're looking for.
        if artist_score < 0.35:
            return 0.0
        score = artist_score * 0.55 + title_score * 0.30
    else:
        # No artist — lean on title but don't trust duration alone.
        score = title_score * 0.65

    score += _duration_bonus(duration_s, candidate.duration_ms)
    score -= _version_penalty(candidate.title)
    return max(0.0, min(1.0, score))
