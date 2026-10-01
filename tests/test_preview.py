"""Preview lifecycle tests without network access or audio output."""
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ytdlp_app.preview import AudioPreview


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.preview = AudioPreview()

    def tearDown(self):
        self.preview.close()
        self.preview._pool.shutdown(wait=True)

    def test_cached_replay_does_not_resolve_or_convert_again(self):
        def command(args, cancel, timeout):
            if timeout == 90:
                Path(args[-1]).write_bytes(b"sample")
            return True

        with patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL') as ydl, \
             patch.object(self.preview, '_command', side_effect=command) as run:
            ydl.return_value.__enter__.return_value.extract_info.return_value = {'url': 'https://audio.test/stream'}
            for token in (1, 2):
                self.preview._run(token, threading.Event(), 'https://youtube.test/watch?v=1', None)
            self.assertEqual(ydl.call_count, 1)
            self.assertEqual(run.call_count, 3)
            self.assertEqual([self.preview.events.get_nowait()[1] for _ in range(4)], ['playing', 'done', 'playing', 'done'])

    def test_cancel_during_resolution_never_plays(self):
        cancel = threading.Event()
        def resolve(*args, **kwargs):
            cancel.set()
            return {'url': 'https://audio.test/stream'}
        with patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL') as ydl, \
             patch.object(self.preview, '_command') as run:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = resolve
            self.preview._run(1, cancel, 'https://youtube.test/watch?v=1', None)
            run.assert_not_called()
            self.assertTrue(self.preview.events.empty())

    def test_stop_terminates_active_process(self):
        process = MagicMock()
        process.poll.return_value = None
        self.preview._process = process
        self.preview.stop()
        process.terminate.assert_called_once()
        self.assertTrue(self.preview._cancel.is_set())
        self.preview._process = None

    def test_missing_player_reports_error(self):
        with patch('ytdlp_app.preview.shutil.which', return_value=None):
            self.preview._run(3, threading.Event(), 'url', None)
        token, state, _ = self.preview.events.get_nowait()
        self.assertEqual((token, state), (3, 'error'))

    def test_rejected_stream_falls_back_to_local_audio_and_caches_clip(self):
        attempts = []
        def command(args, cancel, timeout):
            attempts.append(args)
            if timeout == 90 and len(attempts) == 1:
                raise RuntimeError('HTTP 403')
            if timeout == 90:
                self.assertIn('/audio.webm', args[args.index('-i') + 1])
                Path(args[-1]).write_bytes(b'converted clip')
            return True
        with patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL') as ydl, \
             patch.object(self.preview, '_command', side_effect=command):
            client = ydl.return_value.__enter__.return_value
            client.extract_info.return_value = {'url': 'https://audio.test/stream'}
            client.prepare_filename.return_value = '/temporary/audio.webm'
            self.preview._run(1, threading.Event(), 'https://youtube.test/watch?v=1', None)
            self.assertEqual(client.extract_info.call_args.kwargs, {'download': True})
            fallback_options = ydl.call_args.args[0]
            self.assertIn('progress_hooks', fallback_options)
            self.assertEqual([self.preview.events.get_nowait()[1] for _ in range(3)], ['loading', 'playing', 'done'])
            self.assertEqual(len(list(Path(self.preview._temp.name).iterdir())), 1)

    def test_error_message_explains_failure_without_signed_urls(self):
        with patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL', side_effect=RuntimeError('HTTP 403 https://audio.test/?secret=value')):
            self.preview._run(1, threading.Event(), 'url', None)
        _, state, message = self.preview.events.get_nowait()
        self.assertEqual(state, 'error')
        self.assertIn('HTTP 403', message)
        self.assertNotIn('secret', message)

    def test_catalog_preview_does_not_use_youtube_extractor(self):
        def command(args, cancel, timeout):
            if timeout == 90:
                Path(args[-1]).write_bytes(b'catalog clip')
            return True
        with patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL') as ydl, \
             patch.object(self.preview, '_command', side_effect=command):
            self.preview._run(1, threading.Event(), 'https://audio-ssl.itunes.apple.com/sample.m4a', None, True)
        ydl.assert_not_called()
        self.assertEqual(self.preview.events.get_nowait()[1], 'playing')
        self.assertEqual(self.preview.events.get_nowait()[1], 'done')

    def test_fast_catalog_sample_bypasses_youtube_and_reports_catalog_source(self):
        from ytdlp_app.playlists import PlaylistTrack
        def command(args, cancel, timeout):
            if timeout == 90:
                Path(args[-1]).write_bytes(b'catalog clip')
            return True
        with patch('ytdlp_app.recommendations._catalog_candidates', return_value=[{'previewUrl': 'https://audio-ssl.itunes.apple.com/a.m4a'}]), \
             patch('ytdlp_app.preview.shutil.which', return_value='/usr/bin/afplay'), \
             patch('ytdlp_app.preview.find_ffmpeg', return_value='/bin/ffmpeg'), \
             patch('ytdlp_app.preview._shared_opts', return_value={}), \
             patch('ytdlp_app.preview.yt_dlp.YoutubeDL') as ydl, \
             patch.object(self.preview, '_command', side_effect=command):
            self.preview._run(1, threading.Event(), 'youtube-source', None, catalog_track=PlaylistTrack('', 'Song', 'Artist'))
        ydl.assert_not_called()
        self.preview.events.get_nowait()
        self.assertIn('catalog', self.preview.events.get_nowait()[2])
