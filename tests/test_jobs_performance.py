"""Deterministic queue tests: use events and fake I/O, never download media."""
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from ytdlp_app import jobs, downloader, music_postprocess, search, rate_limit
from ytdlp_app.match_config import get_match_config
from ytdlp_app.sources.base import MusicTrack, MATCH_READY, MATCH_MATCHED


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.changed = threading.Condition()
        self.events = []
        self.release = threading.Event()
        self.queue = jobs.JobQueue(1, self.notify)

    def notify(self, job):
        with self.changed:
            self.events.append((job.id, job.state, job.progress_msg))
            self.changed.notify_all()

    def wait_for(self, predicate):
        with self.changed:
            self.assertTrue(self.changed.wait_for(predicate, timeout=3), self.events)

    def finished(self, job):
        return any(i == job.id and state in (jobs.DONE, jobs.FAILED, jobs.CANCELLED)
                   for i, state, _ in self.events)

    def tearDown(self):
        self.release.set()
        self.queue.shutdown(wait=True)

    def enqueue(self, title, **kwargs):
        return self.queue.enqueue('music', title, url=title, output_dir='/tmp/music-test',
                                  source_title=title, **kwargs)

    @staticmethod
    def download(urls, out_dir, **kwargs):
        path = f'{out_dir}/{urls[0]}.mp3'
        kwargs['reserve_output'](path)
        return downloader.MusicDownloadResult(success=True, output_paths=[path])

    @staticmethod
    def tag(path, **kwargs):
        return music_postprocess.PostprocessResult(final_path=path)

    def test_discovery_matches_then_downloads_in_one_job(self):
        track = MusicTrack('Artist', 'Song', album='Record', source='shazam')
        def match(job, log):
            job.result = [replace(track, youtube_url='matched', match_status=MATCH_MATCHED)]
        with patch.object(self.queue, '_match_tracks', side_effect=match), \
             patch.object(jobs.dl, 'download_music', side_effect=self.download) as download, \
             patch.object(jobs.mp, 'process_track', side_effect=self.tag), \
             patch.object(search, 'find_preferred_audio_url') as rematch:
            job = self.enqueue('Song', match_before_download=True, tracks=[track.to_dict()], prefer_audio=True)
            self.wait_for(lambda: self.finished(job))
        self.assertEqual(job.state, jobs.DONE)
        self.assertEqual(download.call_args.args[0], ['matched'])
        rematch.assert_not_called()

    def test_discovery_missing_match_fails_without_downloading(self):
        track = MusicTrack('Artist', 'Song', source='shazam')
        def match(job, log):
            job.result = [track]
        with patch.object(self.queue, '_match_tracks', side_effect=match), \
             patch.object(jobs.dl, 'download_music') as download:
            job = self.enqueue('Song', match_before_download=True, tracks=[track.to_dict()])
            self.wait_for(lambda: self.finished(job))
        self.assertEqual(job.state, jobs.FAILED)
        self.assertIn('No reliable YouTube match', job.error)
        download.assert_not_called()

    def test_discovery_existing_song_skips_matching_and_download(self):
        track = MusicTrack('Artist', 'Song', source='shazam')
        with patch('ytdlp_app.music_duplicates.check_music_duplicate', return_value=(True, 'Song', 'library')), \
             patch.object(self.queue, '_match_tracks') as match, \
             patch.object(jobs.dl, 'download_music') as download:
            job = self.enqueue('Song', match_before_download=True, skip_existing=True, tracks=[track.to_dict()])
            self.wait_for(lambda: self.finished(job))
        self.assertEqual(job.state, jobs.DONE)
        self.assertTrue(job.result['skipped_duplicate'])
        match.assert_not_called()
        download.assert_not_called()

    def test_playlist_import_failure_marks_job_failed_and_passes_target(self):
        with patch.object(jobs.dl, 'download_music', side_effect=self.download), \
             patch.object(jobs.mp, 'process_track', side_effect=self.tag), \
             patch.object(jobs.am, 'import_to_library', return_value=jobs.am.ImportResult(False, 'playlist removed')) as imp:
            job = self.enqueue('Song', add_to_apple_music=True, apple_music_playlist_id='TARGET')
            self.wait_for(lambda: self.finished(job))
        self.assertEqual(job.state, jobs.FAILED)
        self.assertEqual(imp.call_args.kwargs['playlist_id'], 'TARGET')
        self.assertIn('Downloaded file saved', job.error)

    def test_tagging_does_not_occupy_download_worker(self):
        tagging = threading.Event()
        second_download = threading.Event()

        def download(urls, out_dir, **kwargs):
            if urls[0] == 'second':
                second_download.set()
            return self.download(urls, out_dir, **kwargs)

        def tag(path, **kwargs):
            if path.endswith('/first.mp3'):
                tagging.set()
                self.release.wait(3)
            return self.tag(path, **kwargs)

        with patch.object(jobs.dl, 'download_music', side_effect=download), patch.object(jobs.mp, 'process_track', side_effect=tag):
            first = self.enqueue('first')
            self.assertTrue(tagging.wait(3))
            second = self.enqueue('second')
            self.assertTrue(second_download.wait(3))
            self.assertEqual(first.state, jobs.RUNNING)
            self.wait_for(lambda: self.finished(second))
            self.release.set()
            self.wait_for(lambda: self.finished(first))
            self.assertEqual(first.state, jobs.DONE)
            self.assertIn('tagging', first.timings)
            self.assertIn('download', first.timings)
            self.assertGreaterEqual(first.timings['total'], first.timings['download'])
            self.assertEqual(sum(i == first.id and state == jobs.DONE for i, state, _ in self.events), 1)

    def test_same_output_is_owned_until_tagging_finishes(self):
        tagging = threading.Event()
        second_attempt = threading.Event()
        second_acquired = threading.Event()
        calls = 0

        def download(urls, out_dir, **kwargs):
            nonlocal calls
            calls += 1
            second = calls == 2
            if second:
                second_attempt.set()
            result = self.download(urls, out_dir, **kwargs)
            if second:
                second_acquired.set()
            return result

        def tag(path, **kwargs):
            tagging.set()
            self.release.wait(3)
            return self.tag(path, **kwargs)

        with patch.object(jobs.dl, 'download_music', side_effect=download), patch.object(jobs.mp, 'process_track', side_effect=tag):
            first = self.enqueue('same')
            self.assertTrue(tagging.wait(3))
            second = self.enqueue('same')
            self.assertTrue(second_attempt.wait(3))
            self.assertFalse(second_acquired.is_set())
            self.release.set()
            self.wait_for(lambda: self.finished(first) and self.finished(second))
            self.assertTrue(second_acquired.is_set())
            self.assertEqual(self.queue._output_owners, {})

    def test_tag_failure_is_terminal_failure(self):
        with patch.object(jobs.dl, 'download_music', side_effect=self.download), patch.object(
            jobs.mp, 'process_track', return_value=music_postprocess.PostprocessResult(success=False, message='bad tags')
        ):
            job = self.enqueue('bad')
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.FAILED)
            self.assertEqual(job.error, 'bad tags')
            self.assertEqual(self.queue._output_owners, {})

    def test_cancel_running_tagging_does_not_report_done(self):
        tagging = threading.Event()

        def tag(path, **kwargs):
            tagging.set()
            kwargs['cancel_event'].wait(3)
            return self.tag(path, **kwargs)

        with patch.object(jobs.dl, 'download_music', side_effect=self.download), patch.object(jobs.mp, 'process_track', side_effect=tag):
            job = self.enqueue('cancel')
            self.assertTrue(tagging.wait(3))
            self.queue.cancel(job.id)
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.CANCELLED)

    def test_cancel_waiting_tag_stage_never_tags_that_track(self):
        started = threading.Barrier(3)
        tagged = []

        def tag(path, **kwargs):
            tagged.append(path)
            started.wait(timeout=3)
            self.release.wait(3)
            return self.tag(path, **kwargs)

        with patch.object(jobs.dl, 'download_music', side_effect=self.download), patch.object(jobs.mp, 'process_track', side_effect=tag):
            first = self.enqueue('one')
            second = self.enqueue('two')
            started.wait(timeout=3)
            third = self.enqueue('three')
            self.wait_for(lambda: any(i == third.id and msg == 'Waiting for tagging…' for i, _, msg in self.events))
            self.queue.cancel(third.id)
            self.release.set()
            self.wait_for(lambda: self.finished(first) and self.finished(second) and self.finished(third))
            self.assertEqual(third.state, jobs.CANCELLED)
            self.assertEqual(len(tagged), 2)

    def test_download_failure_never_enters_tag_pool(self):
        with patch.object(jobs.dl, 'download_music', return_value=downloader.MusicDownloadResult(False, message='download failed')), patch.object(jobs.mp, 'process_track') as tag:
            job = self.enqueue('failure')
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.FAILED)
            tag.assert_not_called()

    def test_import_finishes_before_done(self):
        importing = threading.Event()

        def import_track(path, **kwargs):
            importing.set()
            self.release.wait(3)
            return jobs.am.ImportResult(success=True)

        with patch.object(jobs.dl, 'download_music', side_effect=self.download), patch.object(jobs.mp, 'process_track', side_effect=self.tag), patch.object(jobs.am, 'import_to_library', side_effect=import_track):
            job = self.enqueue('import', add_to_apple_music=True)
            self.assertTrue(importing.wait(3))
            self.assertEqual(job.state, jobs.RUNNING)
            self.release.set()
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.DONE)
            self.assertIn('import', job.timings)

    def test_shutdown_during_download_cancels_without_tagging(self):
        downloading = threading.Event()

        def download(*args, **kwargs):
            downloading.set()
            self.release.wait(3)
            return self.download(*args, **kwargs)

        with patch.object(jobs.dl, 'download_music', side_effect=download), patch.object(jobs.mp, 'process_track') as tag:
            job = self.enqueue('shutdown')
            self.assertTrue(downloading.wait(3))
            self.queue.shutdown(wait=False)
            self.release.set()
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.CANCELLED)
            tag.assert_not_called()
            with self.assertRaises(RuntimeError):
                self.enqueue('late')

    def test_matching_overlaps_and_preserves_input_order(self):
        second_finished = threading.Event()
        calls = []

        def match(artist, title, *args, **kwargs):
            calls.append(title)
            if title == 'first':
                self.assertTrue(second_finished.wait(3))
            else:
                second_finished.set()
            return search.SearchResult(title, title, artist, None, None, None, None)

        ready = MusicTrack('Artist', 'ready', youtube_url='ready', match_status=MATCH_READY)
        cfg = replace(get_match_config('balanced'), inter_track_delay_s=0)
        with patch('ytdlp_app.match_config.get_match_config', return_value=cfg), patch.object(search, 'find_youtube_match_for_track', side_effect=match):
            job = self.queue.enqueue('source_match_all', 'match', tracks=[MusicTrack('Artist', 'first'), ready, MusicTrack('Artist', 'second')], results_context='music')
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.DONE, job.error)
            self.assertEqual([t.title for t in job.result], ['first', 'ready', 'second'])
            self.assertEqual([t.match_status for t in job.result], [MATCH_MATCHED, MATCH_READY, MATCH_MATCHED])
            self.assertEqual(set(calls), {'first', 'second'})
            self.assertIn('matching', job.timings)

    def test_cancel_match_stops_scheduling(self):
        started = threading.Event()
        calls = []

        def match(artist, title, *args, **kwargs):
            calls.append(title)
            started.set()
            kwargs['cancel_event'].wait(3)
            return None

        cfg = replace(get_match_config('balanced'), inter_track_delay_s=10)
        with patch('ytdlp_app.match_config.get_match_config', return_value=cfg), patch.object(search, 'find_youtube_match_for_track', side_effect=match):
            job = self.queue.enqueue('source_match_all', 'cancel match', tracks=[MusicTrack('Artist', str(i)) for i in range(10)])
            self.assertTrue(started.wait(3))
            self.queue.cancel(job.id)
            self.wait_for(lambda: self.finished(job))
            self.assertEqual(job.state, jobs.CANCELLED)
            self.assertEqual(calls, ['0'])


class RequestPacingTests(unittest.TestCase):
    def test_concurrent_match_settings_do_not_leak(self):
        barrier = threading.Barrier(2)
        seen = {}

        def find(*args, **kwargs):
            barrier.wait(timeout=3)
            seen[kwargs['match_quality']] = rate_limit.get_sleep_interval_requests()
            return None

        def run(quality):
            search.find_youtube_match_for_track('Artist', 'Song', match_quality=quality)
            self.assertIsNone(rate_limit.get_sleep_interval_requests())

        with patch.object(search, '_find_youtube_match_for_track', side_effect=find):
            with jobs.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, quality) for quality in ('fast', 'accurate')]
                for future in futures:
                    future.result(timeout=3)
        self.assertEqual(seen, {'fast': 1.0, 'accurate': 0.5})

    def test_enrichment_workers_inherit_request_pacing(self):
        seen = []
        result = search.SearchResult('https://www.youtube.com/watch?v=abcdefghijk', 'Song', '', None, None, None, None)

        def fetch(*args, **kwargs):
            seen.append(rate_limit.get_sleep_interval_requests())
            return {'uploader': 'Artist'}

        with rate_limit.request_interval(0.75), patch.object(search, '_extract_flat_video_info', side_effect=fetch):
            enriched = search._enrich_flat_results([result], cookies_path=None, cancel_event=None, progress=lambda msg: None)
        self.assertEqual(seen, [0.75])
        self.assertEqual(enriched[0].uploader, 'Artist')

    def test_request_interval_restored_on_exception(self):
        with rate_limit.request_interval(0.25):
            with self.assertRaises(ValueError), rate_limit.request_interval(1):
                raise ValueError('failed')
            self.assertEqual(rate_limit.get_sleep_interval_requests(), 0.25)
        self.assertIsNone(rate_limit.get_sleep_interval_requests())


if __name__ == '__main__':
    unittest.main()
