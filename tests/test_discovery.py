import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from ytdlp_app.discovery import identify, find_similar, parse_track


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        from ytdlp_app.discovery import _SIMILAR_CACHE
        _SIMILAR_CACHE.clear()

    def test_similar_cache_reuses_results_and_expires(self):
        raw = {'tracks': [{'key': '2', 'title': 'Song'}]}
        with patch('ytdlp_app.discovery._request', new=AsyncMock(return_value=raw)) as request, \
             patch('ytdlp_app.discovery.time.monotonic', return_value=10) as clock:
            first = find_similar('1', threading.Event())
            second = find_similar('1', threading.Event())
            self.assertEqual(first, second)
            self.assertEqual(request.call_count, 1)
            first.clear()
            self.assertEqual(len(find_similar('1', threading.Event())), 1)
            clock.return_value = 311
            find_similar('1', threading.Event())
            self.assertEqual(request.call_count, 2)

    def test_apple_metadata_excludes_subscription_link(self):
        track = parse_track({'key': '12', 'hub': {'options': [{'actions': [
            {'uri': 'https://music.apple.com/subscribe'},
            {'uri': 'https://music.apple.com/us/album/example/11?i=22'},
        ]}]}})
        self.assertEqual(track.apple_id, '22')
        self.assertIn('?i=22', track.apple_url)
        self.assertEqual(parse_track({'hub': {'options': [{'actions': [{'uri': 'https://music.apple.com/subscribe'}]}]}}).apple_url, '')

    def test_local_recognition_never_changes_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'song.wav'
            path.write_bytes(b'original')
            with patch('ytdlp_app.discovery._request', new=AsyncMock(return_value={'track': {'key': '1', 'title': 'Song'}})):
                result = identify(str(path), is_link=False, cookies=None, cancel=threading.Event())
            self.assertEqual(result.title, 'Song')
            self.assertEqual(path.read_bytes(), b'original')

    def test_no_match_has_actionable_message(self):
        with tempfile.NamedTemporaryFile() as audio, patch('ytdlp_app.discovery._request', new=AsyncMock(return_value={'matches': []})):
            with self.assertRaisesRegex(ValueError, 'No song recognized'):
                identify(audio.name, is_link=False, cookies=None, cancel=threading.Event())

    def test_related_deduplicates_and_excludes_seed(self):
        raw = {'tracks': [{'key': '1'}, {'key': '2'}, {'key': '2'}, {}]}
        with patch('ytdlp_app.discovery._request', new=AsyncMock(return_value=raw)):
            self.assertEqual([t.key for t in find_similar('1', threading.Event())], ['2'])

    def test_cancel_does_not_call_service(self):
        cancel = threading.Event()
        cancel.set()
        with patch('ytdlp_app.discovery._request') as request:
            with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
                find_similar('1', cancel)
            request.assert_not_called()

    def test_rejects_non_youtube_link(self):
        with self.assertRaisesRegex(ValueError, 'YouTube'):
            identify('https://example.com/audio', is_link=True, cookies=None, cancel=threading.Event())

    def test_enrichment_uses_exact_catalog_ids_and_preserves_missing_results(self):
        from ytdlp_app.discovery import DiscoveredTrack, enrich_tracks
        tracks = [DiscoveredTrack('1', 'Song', 'Artist', '', apple_id='22'),
                  DiscoveredTrack('2', 'Unknown', 'Artist', '')]
        rows = [{'kind': 'song', 'trackId': 22, 'collectionName': 'Album', 'trackTimeMillis': 201000,
                 'primaryGenreName': 'Pop', 'releaseDate': '2020-01-02T00:00:00Z',
                 'trackExplicitness': 'explicit', 'trackNumber': 4},
                {'kind': 'song', 'trackId': 33, 'collectionName': 'Wrong album'}]
        with patch('ytdlp_app.metadata.itunes._fetch_results', return_value=rows) as fetch:
            result = enrich_tracks(tracks, threading.Event())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result[0].album, 'Album')
        self.assertEqual(result[0].duration_s, 201)
        self.assertEqual(result[0].release_date, '2020-01-02')
        self.assertEqual(result[1], tracks[1])
