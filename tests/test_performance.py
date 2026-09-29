"""Offline regression tests for request reuse and progress throttling."""
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from ytdlp_app import downloader
from ytdlp_app.metadata import itunes, lyrics


def response(payload):
    result = MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


class MetadataRequests(unittest.TestCase):
    def setUp(self):
        itunes._results_cache.clear()

    def tearDown(self):
        itunes._results_cache.clear()

    def test_catalog_reuse_and_mutation_isolation(self):
        with patch.object(itunes.urllib.request, 'urlopen', return_value=response(
            {'results': [{'trackName': 'Song', 'nested': {'value': 1}}]}
        )) as fetch:
            first = itunes._fetch_results('https://example.test/catalog')
            first[0]['nested']['value'] = 2
            for _ in range(20):
                self.assertEqual(itunes._fetch_results('https://example.test/catalog')[0]['nested']['value'], 1)
            self.assertEqual(fetch.call_count, 1)

    def test_expired_catalog_is_refetched(self):
        with patch.object(itunes.time, 'monotonic', return_value=0) as clock, patch.object(
            itunes.urllib.request, 'urlopen', return_value=response({'results': [{'id': 1}]})
        ) as fetch:
            itunes._fetch_results('https://example.test/catalog')
            clock.return_value = itunes._RESULTS_CACHE_TTL_S + 1
            itunes._fetch_results('https://example.test/catalog')
            self.assertEqual(fetch.call_count, 2)

    def test_failure_is_retried(self):
        with patch.object(itunes.urllib.request, 'urlopen', side_effect=[
            OSError('temporary failure'), response({'results': [{'id': 1}]})
        ]) as fetch:
            self.assertEqual(itunes._fetch_results('https://example.test/catalog'), [])
            self.assertEqual(itunes._fetch_results('https://example.test/catalog'), [{'id': 1}])
            self.assertEqual(fetch.call_count, 2)

    def test_cache_is_bounded(self):
        with patch.object(itunes, '_RESULTS_CACHE_MAX', 2), patch.object(
            itunes.urllib.request, 'urlopen', return_value=response({'results': [{'id': 1}]})
        ):
            for name in ('one', 'two', 'three'):
                itunes._fetch_results('https://example.test/' + name)
            self.assertEqual(len(itunes._results_cache), 2)
            self.assertNotIn('https://example.test/one', itunes._results_cache)

    def test_lyrics_search_reuses_payload(self):
        row = {'id': 7, 'artistName': 'Artist', 'trackName': 'Song', 'plainLyrics': 'Words'}
        with patch.object(lyrics.urllib.request, 'urlopen', return_value=response([row])) as fetch:
            self.assertEqual(lyrics._search_lyrics('Artist', 'Song', None).plain, 'Words')
            self.assertEqual(fetch.call_count, 1)

    def test_lyrics_id_fallback_is_preserved(self):
        row = {'id': 7, 'artistName': 'Artist', 'trackName': 'Song'}
        with patch.object(lyrics.urllib.request, 'urlopen', side_effect=[
            response([row]), response({'plainLyrics': 'Words'})
        ]) as fetch:
            self.assertEqual(lyrics._search_lyrics('Artist', 'Song', None).plain, 'Words')
            self.assertEqual(fetch.call_count, 2)


class ProgressEvents(unittest.TestCase):
    def test_burst_is_throttled_but_finish_and_cancel_are_immediate(self):
        opts = {}
        updates = MagicMock()
        cancel = threading.Event()
        downloader._attach_progress(opts, lambda msg: None, updates, cancel)
        hook = opts['progress_hooks'][0]
        with patch.object(downloader.time, 'monotonic', return_value=10):
            for downloaded in range(1000):
                hook({'status': 'downloading', 'downloaded_bytes': downloaded, 'total_bytes': 1000})
            self.assertEqual(updates.call_count, 1)
            hook({'status': 'finished'})
            self.assertEqual(updates.call_args.args[0], 100)
            self.assertEqual(updates.call_count, 2)
            cancel.set()
            with self.assertRaises(downloader._Cancelled):
                hook({'status': 'downloading'})


if __name__ == '__main__':
    unittest.main()
