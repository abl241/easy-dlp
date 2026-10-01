import plistlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from ytdlp_app import playlists, recommendations
from ytdlp_app.discovery import DiscoveredTrack, parse_track


class PlaylistTests(unittest.TestCase):
    def test_xml_preserves_playlist_order_and_unicode_without_confusing_ids(self):
        data = {'Tracks': {'1': {'Name': 'Été\nAgain', 'Artist': 'Artist', 'Persistent ID': 'AB12', 'Total Time': 210000},
                           '2': {'Name': 'Second', 'Artist': 'Other', 'Store ID': 12345}},
                'Playlists': [{'Name': 'Favorites', 'Playlist Items': [{'Track ID': 2}, {'Track ID': 1}, {'Track ID': 999}]}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'playlist.xml'
            path.write_bytes(plistlib.dumps(data))
            result = playlists.import_xml(path)
        self.assertEqual([t.title for t in result[0].tracks], ['Second', 'Été\nAgain'])
        self.assertEqual(result[0].tracks[1].apple_id, '')
        self.assertEqual(result[0].tracks[1].duration_s, 210)
        self.assertEqual(result[0].tracks[0].apple_id, '12345')

    def test_catalog_match_rejects_cover_and_wrong_version(self):
        track = playlists.PlaylistTrack('1', 'Song', 'Original', duration_s=200)
        base = {'wrapperType': 'track', 'kind': 'song', 'trackName': 'Song', 'artistName': 'Original', 'trackTimeMillis': 200000}
        rows = [dict(base, artistName='Cover Artist'), dict(base, trackName='Song (Live)'), dict(base, trackTimeMillis=240000), base]
        self.assertEqual(recommendations._matching_catalog(track, rows), [base])

    def test_song_resolves_catalog_id_before_related_lookup(self):
        track = playlists.PlaylistTrack('local123', 'Song', 'Artist')
        row = {'trackId': 456, 'trackName': 'Song', 'artistName': 'Artist', 'previewUrl': 'https://audio-ssl.itunes.apple.com/sample.m4a'}
        with patch.object(recommendations, '_catalog_candidates', return_value=[row]), \
             patch.object(recommendations, '_map_apple_id', return_value='789') as mapping, \
             patch.object(recommendations.discovery, 'find_similar', return_value=[]) as similar:
            seed, result = recommendations.for_song(track, threading.Event())
        mapping.assert_called_once_with('456')
        self.assertEqual(similar.call_args.args[0], '789')
        self.assertEqual(seed.apple_id, '456')

    def test_playlist_ranking_excludes_existing_songs_deduplicates_and_limits_artists(self):
        tracks = [playlists.PlaylistTrack(str(i), f'Seed {i}', 'Seeds') for i in range(12)]
        def similar(track, cancel):
            seed = DiscoveredTrack(track.library_id, track.title, track.artist, '')
            related = [DiscoveredTrack('0', 'Seed 0', 'Seeds', ''),
                       *[DiscoveredTrack(str(100+i), f'New {i}', 'Same artist', '', preview_url='https://example.test/a') for i in range(6)]]
            return seed, related
        with patch.object(recommendations, 'for_song', side_effect=similar) as request:
            result = recommendations.for_playlist(tracks, threading.Event())
        self.assertEqual(request.call_count, 8)
        self.assertEqual(result.matched, 8)
        self.assertEqual(len(result.suggestions), 3)
        self.assertEqual(len(result.suggestions[0].because), 8)
        self.assertNotIn('Seed 0', [s.track.title for s in result.suggestions])

    def test_partial_failure_is_reported_and_cancel_is_not_swallowed(self):
        tracks = [playlists.PlaylistTrack('1', 'A', 'Artist'), playlists.PlaylistTrack('2', 'B', 'Artist')]
        with patch.object(recommendations, 'for_song', side_effect=[ValueError('not mapped'), (DiscoveredTrack('2', 'B', 'Artist', ''), [])]):
            result = recommendations.for_playlist(tracks, threading.Event())
        self.assertEqual(result.matched, 1)
        self.assertEqual(len(result.skipped), 1)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
            recommendations.for_playlist(tracks, cancel)

    def test_parse_preview_only_uses_catalog_media(self):
        data = {'hub': {'actions': [{'type': 'uri', 'uri': 'https://audio-ssl.itunes.apple.com/sample.m4a'}]}}
        self.assertTrue(parse_track(data).preview_url.endswith('sample.m4a'))
        self.assertEqual(parse_track({'hub': {'actions': [{'type': 'uri', 'uri': 'https://music.apple.com/subscribe'}]}}).preview_url, '')

    def test_library_and_same_named_user_playlist_are_distinct(self):
        with patch.object(playlists, '_music_read', return_value=[
            {'id': 'LIB', 'name': 'Music', 'special_kind': 'Music'},
            {'id': 'USER', 'name': 'music', 'special_kind': 'none'}]):
            library, user = playlists.list_playlists(threading.Event())
        self.assertEqual(library.display_name, 'Music (Library)')
        self.assertEqual(user.display_name, 'music')
        with patch.object(playlists, '_music_read', return_value=[]) as read:
            playlists.load_tracks(user, threading.Event())
        self.assertEqual(read.call_args.args[0], ['USER'])
