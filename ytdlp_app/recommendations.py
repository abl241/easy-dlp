"""Catalog-backed seed resolution and bounded, explainable playlist suggestions."""
from dataclasses import dataclass
from functools import lru_cache
import json
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import discovery
from .metadata.itunes import _artist_similarity, _fetch_results


def identity(title, artist):
    def norm(text):
        text = unicodedata.normalize('NFKD', text).casefold()
        return ''.join(c for c in text if c.isalnum())
    return norm(title), norm(artist)


def _matching_catalog(track, rows):
    candidates = []
    for row in rows:
        if row.get('wrapperType') != 'track' or row.get('kind') != 'song':
            continue
        # Preserve live/remix/clean/version labels. No guessed cover matches.
        if identity(row.get('trackName', ''), '')[0] != identity(track.title, '')[0]:
            continue
        if not track.artist or _artist_similarity(row.get('artistName', ''), track.artist) < .8:
            continue
        duration = (row.get('trackTimeMillis') or 0) / 1000
        if track.duration_s and duration and abs(duration - track.duration_s) > 12:
            continue
        candidates.append(row)
    candidates.sort(key=lambda row: (identity(row.get('collectionName', ''), '')[0] == identity(track.album, '')[0], bool(row.get('previewUrl'))), reverse=True)
    return candidates


@lru_cache(maxsize=512)
def _map_apple_id(apple_id):
    # Adapter for the mapping endpoint in upstream ShazamIO; absent in 0.8.1.
    # Resolve singly: bulk responses may use an alternate release's Apple ID.
    if not apple_id.isdigit():
        raise ValueError('Invalid Apple Music catalog ID.')
    request = Request('https://www.shazam.com/services/sd/s/a2st/US/en-US/' + apple_id,
                      headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read())
    key = str(data.get(apple_id) or '')
    if not key.isdigit():
        raise ValueError('Shazam has no mapping for this catalog release.')
    return key


def _catalog_candidates(track):
    if getattr(track, "apple_id", ""):
        rows = _fetch_results('https://itunes.apple.com/lookup?' + urlencode({'id': track.apple_id, 'country': 'US'}))
        candidates = _matching_catalog(track, rows)
        if candidates:
            return candidates
    rows = _fetch_results('https://itunes.apple.com/search?' + urlencode({
        'term': f'{track.artist} {track.title}', 'entity': 'song', 'limit': 25, 'country': 'US'}))
    return _matching_catalog(track, rows)


def resolve_track(track, cancel):
    discovery.check_cancel(cancel)
    candidates = _catalog_candidates(track)
    for row in candidates[:3]:
        discovery.check_cancel(cancel)
        apple_id = str(row.get('trackId') or '')
        try:
            key = _map_apple_id(apple_id)
        except ValueError:
            continue
        discovery.check_cancel(cancel)
        return discovery.DiscoveredTrack(key, row['trackName'], row['artistName'],
                                         'https://www.shazam.com/track/' + key,
                                         row.get('trackViewUrl', ''), apple_id,
                                         row.get('previewUrl', ''))
    raise ValueError('No reliable catalog/Shazam match for this song. Try identifying its audio in Identify.')


@dataclass(frozen=True)
class Suggestion:
    track: discovery.DiscoveredTrack
    because: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationResult:
    suggestions: tuple[Suggestion, ...]
    attempted: int
    matched: int
    skipped: tuple[str, ...]
    seeds: tuple[str, ...] = ()


def for_song(track, cancel):
    seed = resolve_track(track, cancel)
    return seed, discovery.find_similar(seed.key, cancel)


def for_playlist(tracks, cancel, progress=lambda _: None, max_seeds=8):
    unique = list({identity(t.title, t.artist): t for t in tracks}.values())
    if not unique:
        return RecommendationResult((), 0, 0, ())
    count = min(max_seeds, len(unique))
    seeds = [unique[round(i * (len(unique) - 1) / max(1, count - 1))] for i in range(count)]
    existing = {identity(t.title, t.artist) for t in tracks}
    existing_ids = {t.apple_id for t in tracks if t.apple_id}
    collected, skipped, matched, seed_keys = {}, [], 0, set()
    for index, track in enumerate(seeds):
        discovery.check_cancel(cancel)
        progress(f'Finding suggestions from {index + 1}/{count}: {track.artist} — {track.title}')
        try:
            seed, related = for_song(track, cancel)
            seed_keys.add(seed.key)
            matched += 1
        except Exception as error:
            discovery.check_cancel(cancel)
            skipped.append(f'{track.artist} — {track.title}: {error}')
            continue
        reason = f'{track.artist} — {track.title}'
        for result in related:
            key = identity(result.title, result.artist)
            if key in existing or (result.apple_id and result.apple_id in existing_ids):
                continue
            if key not in collected:
                collected[key] = (result, [])
            if reason not in collected[key][1]:
                collected[key][1].append(reason)
    ranked = sorted(collected.values(), key=lambda pair: -len(pair[1]))
    artist_counts, suggestions = {}, []
    for track, reasons in ranked:
        if track.key in seed_keys:
            continue
        artist = identity('', track.artist)[1]
        if artist_counts.get(artist, 0) >= 3:
            continue
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        suggestions.append(Suggestion(track, tuple(reasons)))
        if len(suggestions) == 40:
            break
    return RecommendationResult(tuple(suggestions), count, matched, tuple(skipped), tuple(f'{t.artist} — {t.title}' for t in seeds))
