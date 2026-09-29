"""Native UI integration tests. Run with a graphical desktop session.

No network requests, downloads, or personal settings are used.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import customtkinter as ctk
from PIL import Image

from ytdlp_app.gui import App, _ResultRow, _MusicTrackRow
from ytdlp_app.jobs import Job, RUNNING, DONE
from ytdlp_app.search import SearchResult
from ytdlp_app.settings import Settings
from ytdlp_app.sources.base import MusicTrack, MATCH_FAILED
from ytdlp_app.ui import artwork_image


class DesktopUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = App(Settings(Path(cls.temp.name) / 'settings.json'))
        cls.app.update()
        cls.errors = []
        cls.app.report_callback_exception = lambda *args: cls.errors.append(args)

    @classmethod
    def tearDownClass(cls):
        cls.app._on_close()
        cls.temp.cleanup()
        if cls.errors:
            raise AssertionError(cls.errors)

    def pump(self, seconds=.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(.005)

    def result(self, i=0, **kwargs):
        return SearchResult(url=f'https://www.youtube.com/watch?v=test{i}',
                            title='A long title ' * 12, uploader='Artist', duration_s=210,
                            view_count=None, upload_date=None, thumbnail_url=None, **kwargs)

    def test_navigation_retains_search_and_rows(self):
        app = self.app
        app.tabs.set('Music')
        app.music_search_var.set('Keep this query')
        app.music_results = [self.result(i) for i in range(8)]
        app._music_render_results()
        self.pump()
        rows = list(app._music_result_rows)
        app.tabs.set('Video')
        app.tabs.set('Settings')
        app.tabs.set('Music')
        self.pump()
        self.assertEqual(app.music_search_var.get(), 'Keep this query')
        self.assertEqual(rows, app._music_result_rows)
        self.assertTrue(app.music_search_entry.winfo_ismapped())
        self.assertFalse(app._music_empty.winfo_ismapped())

    def test_unified_input_routes_without_hidden_track_list(self):
        app = self.app
        app.music_track_list_box.insert('1.0', 'Unrelated - Saved draft')
        with patch.object(app.jobs, 'enqueue', return_value=MagicMock(id=88)) as enqueue:
            app.music_search_var.set('https://open.spotify.com/playlist/example')
            app._submit_music_input()
            args = enqueue.call_args.kwargs
            self.assertEqual(args['kind'], 'source_resolve')
            self.assertEqual(args['platform'], 'spotify')
            self.assertEqual(args['text'], '')
            app.music_search_var.set('https://www.youtube.com/watch?v=example')
            app._submit_music_input()
            self.assertEqual(enqueue.call_args.kwargs['kind'], 'resolve')
            app.music_search_var.set('an artist')
            app._submit_music_input()
            self.assertEqual(enqueue.call_args.kwargs['kind'], 'search')
            app.search_var.set('https://www.youtube.com/playlist?list=example')
            app._submit_video_input()
            self.assertEqual(enqueue.call_args.kwargs['kind'], 'resolve')
        self.assertIn('Unrelated', app.music_track_list_box.get('1.0', 'end'))

    def test_repeated_submit_does_not_overlap_jobs(self):
        job = Job(id=98, kind='search', label='Searching', params={'results_context': 'music'})
        with patch.object(self.app.jobs, 'active', return_value=[job]), patch.object(self.app.jobs, 'enqueue') as enqueue:
            self.app._submit_music_input()
            enqueue.assert_not_called()

    def test_bulk_import_and_escape(self):
        app = self.app
        app.tabs.set('Music')
        app.music_source_tabs.set('Paste Link')
        self.pump()
        self.assertTrue(app.music_paste_box.winfo_ismapped())
        self.assertTrue(app.music_search_entry.winfo_ismapped())
        app._activity_collapsed = True
        app._dismiss_detail()
        self.pump()
        self.assertFalse(app.music_paste_box.winfo_ismapped())
        app._open_activity('recent')
        self.pump()
        self.assertTrue(app.recent_frame.winfo_ismapped())
        app._dismiss_detail()
        self.assertTrue(app._activity_collapsed)

    def test_progress_does_not_resize_workspace(self):
        app = self.app
        app._activity_collapsed = True
        app._apply_activity_dock_layout()
        self.pump()
        before = app.tabs.winfo_height()
        job = Job(id=99, kind='audio', label='Test download', params={})
        job.state, job.progress_pct = RUNNING, 45
        app._handle_job_update(job)
        self.pump(.2)
        self.assertTrue(app._activity_collapsed)
        self.assertEqual(app.tabs.winfo_height(), before)
        self.assertAlmostEqual(app._summary_progress.get(), .45, places=2)
        job.state = DONE
        app._handle_job_update(job)
        self.pump()
        self.assertNotIn(99, app._summary_jobs)
        self.assertEqual(app._summary_progress.get(), 0)

    def test_rows_in_both_themes_and_minimum_window(self):
        app = self.app
        app.geometry('1080x720')
        for theme in ('light', 'dark'):
            ctk.set_appearance_mode(theme)
            for kind in ('track', 'album', 'playlist'):
                row = _ResultRow(app.music_results_frame, self.result(kind=kind), app, mode='music')
                row._apply_thumb(Image.new('RGB', (120, 70)))
                row.set_collection_busy(True)
                row.set_collection_busy(False)
                self.pump(.02)
                self.assertEqual(row._ctk_image.cget('size'), (56, 56))
                row.destroy()
            row = _MusicTrackRow(app.music_results_frame, MusicTrack(artist='Artist', title='Track', match_status=MATCH_FAILED), app, track_index=0)
            self.pump(.02)
            row.destroy()
        ctk.set_appearance_mode('system')

    def test_reduced_motion_has_no_pending_animation(self):
        app = self.app
        app.settings.set('reduce_motion', True)
        app._summary_progress.move_to(.8)
        self.assertEqual(app._summary_progress.get(), .8)
        self.assertIsNone(app._summary_progress._timer)
        app.settings.set('reduce_motion', False)
        app._summary_progress.move_to(0)

    def test_artwork_preserves_aspect_ratio(self):
        image = artwork_image(Image.new('RGB', (160, 90), 'red'), (56, 56))
        self.assertEqual(image.size, (56, 56))
        self.assertEqual(image.getpixel((28, 28)), (255, 0, 0))
        self.assertNotEqual(image.getpixel((28, 0)), (255, 0, 0))

    def test_search_hint_and_keyboard_focus(self):
        app = self.app
        app.tabs.set('Music')
        app.music_search_var.set('')
        app.search_entry.focus_set()
        self.pump()
        app.music_search_entry._sync_hint()
        self.assertTrue(app.music_search_entry.hint.place_info())
        app._focus_search()
        self.pump()
        self.assertFalse(app.music_search_entry.hint.place_info())
        app.music_search_var.set('keyboard search')
        self.assertEqual(app.music_search_entry.get(), 'keyboard search')

    def test_empty_state_returns_after_clearing(self):
        app = self.app
        app.tabs.set('Music')
        app.music_tracks = []
        app.music_results = [self.result()]
        app._music_render_results()
        self.pump()
        self.assertFalse(app._music_empty.place_info())
        app._music_clear_results(confirm=False)
        self.pump()
        self.assertTrue(app._music_empty.place_info())


if __name__ == '__main__':
    unittest.main()
