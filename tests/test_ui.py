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
        app.music_source_tabs.set('Search YouTube')
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
        app.settings.set('music_track_list', 'Unrelated - Saved draft')
        self.assertFalse(hasattr(app, 'music_track_list_box'))
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
        self.assertEqual(app.settings.get('music_track_list'), 'Unrelated - Saved draft')

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
        self.assertFalse(app.music_search_entry.winfo_ismapped())
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

    def test_import_preview_preserves_bulk_draft(self):
        app = self.app
        draft = 'https://youtu.be/saved'
        app.music_paste_box.delete('1.0', 'end')
        app.music_paste_box.insert('1.0', draft)
        app.settings.set('music_paste_urls', draft)
        with patch.object(app.jobs, 'enqueue', return_value=MagicMock(id=100)) as enqueue:
            app._music_do_resolve(single_url='https://open.spotify.com/track/example')
            args = enqueue.call_args.kwargs
            self.assertEqual(args['kind'], 'source_resolve')
            self.assertTrue(args['import_preview'])
            self.assertFalse(args.get('auto_download', False))
        self.assertEqual(app.music_paste_box.get('1.0', 'end').strip(), draft)
        self.assertEqual(app.settings.get('music_paste_urls'), draft)

    def test_import_errors_are_inline_and_start_no_job(self):
        app = self.app
        app.music_paste_box.delete('1.0', 'end')
        app.music_paste_box.insert('1.0', 'https://youtu.be/valid\nnot a link')
        with patch.object(app.jobs, 'enqueue') as enqueue:
            app._music_do_resolve()
            enqueue.assert_not_called()
        self.assertIn('Line 2', app._music_import_feedback.cget('text'))
        self.assertEqual(app.music_source_tabs.get(), 'Paste Link')

    def test_successful_import_moves_to_match_review_without_download(self):
        app = self.app
        app._music_auto_download = False
        app.music_source_tabs.set('Paste Link')
        job = Job(id=101, kind='source_resolve', label='Import Spotify', params={
            'results_context': 'music', 'import_preview': True,
        })
        job.state = DONE
        job.result = [MusicTrack(artist='Artist', title='Track')]
        with patch.object(app.jobs, 'enqueue', return_value=MagicMock(id=102)) as enqueue:
            app._handle_job_update(job)
            enqueue.assert_not_called()
            self.assertEqual(app.music_source_tabs.get(), 'Search YouTube')
            self.assertEqual(app._music_download_all_btn.cget('text'), 'Find matches')
            app._music_primary_action()
            self.assertEqual(enqueue.call_args.kwargs['kind'], 'source_match_all')
            self.assertFalse(enqueue.call_args.kwargs.get('auto_download', False))
        app._music_showing_tracks = False
        app.music_tracks = []

    def test_primary_action_explains_input_and_disables_empty_state(self):
        app = self.app
        app.music_search_var.set('https://youtu.be/example')
        self.assertEqual(app._music_submit_btn.cget('text'), 'Preview link')
        app.music_search_var.set('a song')
        self.assertEqual(app._music_submit_btn.cget('text'), 'Search')
        app.music_search_var.set('')
        self.assertEqual(app._music_submit_btn.cget('state'), 'disabled')

    def test_opening_import_does_not_clear_existing_results(self):
        app = self.app
        app.music_results = [self.result()]
        app._music_showing_tracks = False
        app._music_render_results()
        self.pump()
        row = app._music_result_rows[0]
        app._music_new_link()
        self.pump()
        self.assertEqual(app.music_results, [self.result()])
        self.assertIs(app._music_result_rows[0], row)
        self.assertEqual(app.music_source_tabs.get(), 'Paste Link')

    def test_source_modes_show_only_the_active_input_and_keep_drafts(self):
        app = self.app
        app.tabs.set('Music')
        app.music_search_var.set('saved search')
        app.music_paste_box.delete('1.0', 'end')
        app.music_paste_box.insert('1.0', 'https://youtu.be/saved')
        app.music_source_tabs.set('Paste Link')
        self.pump()
        self.assertFalse(app.music_search_entry.winfo_ismapped())
        self.assertTrue(app.music_paste_box.winfo_ismapped())
        self.assertEqual(app._music_import_btn.cget('text'), 'Import tracks')
        app._focus_search()
        self.pump()
        self.assertTrue(app.music_search_entry.winfo_ismapped())
        self.assertFalse(app.music_paste_box.winfo_ismapped())
        self.assertEqual(app.music_search_var.get(), 'saved search')
        self.assertEqual(app.music_paste_box.get('1.0', 'end').strip(), 'https://youtu.be/saved')
        app.tabs.set('Video')
        app.source_tabs.set('Paste URLs')
        self.pump()
        self.assertFalse(app.search_entry.winfo_ismapped())
        self.assertTrue(app.paste_box.winfo_ismapped())
        app.source_tabs.set('Search YouTube')
        app.tabs.set('Music')

    def test_music_preview_defaults_to_catalog_with_explicit_youtube_option(self):
        app = self.app
        for mode in ('music', 'download'):
            row = _ResultRow(app.music_results_frame, self.result(), app, mode=mode)
            self.assertIsNotNone(row._preview_btn)
            with patch.object(app._audio_preview, 'play', return_value=321) as play, \
                 patch.object(app._audio_preview, 'stop') as stop:
                row._preview_btn.invoke()
                if mode == 'music':
                    play.assert_called_once_with(row.result.url, catalog_track=row._preview_btn.catalog_track)
                    row._preview_btn.invoke()  # stop catalog preview
                    play.reset_mock()
                    stop.reset_mock()
                    row._preview_btn._toggle(catalog=False)
                    play.assert_called_once_with(row.result.url, app.settings.get('cookies_path') or None)
                else:
                    play.assert_called_once_with(row.result.url, app.settings.get('cookies_path') or None)
                row.destroy()
                stop.assert_called_once()
        collection = _ResultRow(app.music_results_frame, self.result(kind='album'), app, mode='music')
        self.assertIsNone(collection._preview_btn)
        collection.destroy()

    def test_identify_and_related_results_are_retained(self):
        from ytdlp_app.discovery import DiscoveredTrack
        app = self.app
        page = app.identify_page
        app.tabs.set('Identify')
        page.mode.set('Audio file')
        page.source.set('/mock/song.mp3')
        song = DiscoveredTrack('123', 'Song', 'Artist', 'https://www.shazam.com/track/123')
        with patch('ytdlp_app.discovery.identify', return_value=song):
            page.identify_btn.invoke()
            self.pump(.3)
        self.assertEqual(page._track, song)
        self.assertEqual(page.similar_btn.cget('state'), 'normal')
        with patch('ytdlp_app.discovery.find_similar', return_value=[song]):
            page.similar_btn.invoke()
            self.pump(.3)
        self.assertIn('1 related songs', page.status.cget('text'))
        app.tabs.set('Music')
        app.tabs.set('Identify')
        self.assertEqual(page._track, song)
        app.tabs.set('Music')

    def test_listen_stop_identify_and_remove_recording(self):
        import threading
        from ytdlp_app.discovery import DiscoveredTrack
        from ytdlp_app import capture
        page = self.app.identify_page
        self.app.tabs.set('Identify')
        page.mode.set('Microphone')
        page._mode_changed('Microphone')
        paths = []
        def record(source, path, *, stop, cancel, progress):
            paths.append(path)
            path.write_bytes(b'test recording')
            progress('● Listening… 20s remaining')
            if not stop.wait(2):
                raise RuntimeError('Test did not stop recording')
        song = DiscoveredTrack('12', 'Recorded song', 'Artist', '')
        with patch.object(capture, 'record', side_effect=record), \
             patch('ytdlp_app.discovery.identify', return_value=song) as recognize:
            page.identify_btn.invoke()
            self.pump(.3)
            self.assertEqual(page.identify_btn.cget('text'), 'Stop & identify')
            self.assertFalse(page.entry.winfo_ismapped())
            page.identify_btn.invoke()
            self.pump(.5)
            recognize.assert_called_once()
            self.assertEqual(page._track, song)
            self.assertEqual(page.identify_btn.cget('text'), 'Listen')
        self.assertFalse(paths[0].exists())
        page.mode.set('Audio file')
        page._mode_changed('Audio file')
        self.app.tabs.set('Music')

    def test_playlist_browse_filter_recommend_and_follow_similar_song(self):
        from ytdlp_app import playlists, recommendations
        from ytdlp_app.discovery import DiscoveredTrack
        from ytdlp_app.discovery_widgets import DiscoveryRow
        app = self.app
        page = app.playlists_page
        app.tabs.set('Playlists')
        songs = tuple(playlists.PlaylistTrack(str(i), f'Song {i}', 'Artist') for i in range(60))
        library = [playlists.Playlist('one', 'Test playlist', songs)]
        with patch.object(playlists, 'list_playlists', return_value=library):
            page.load_btn.invoke()
            self.pump(.5)
        self.assertEqual(page._tracks, [])
        page._select(next(iter(page._playlists)))
        self.pump(.3)
        self.assertEqual(len(page._tracks), 60)
        self.assertEqual(page._shown, 50)
        page.song_filter.set('Song 59')
        self.pump()
        self.assertEqual(len(page._filtered_tracks), 1)
        seed = DiscoveredTrack('12', 'Song 59', 'Artist', '')
        suggestion = DiscoveredTrack('34', 'New song', 'Another artist', 'https://www.shazam.com/track/34', preview_url='https://audio-ssl.itunes.apple.com/sample.m4a')
        with patch.object(recommendations, 'for_song', return_value=(seed, [suggestion])):
            page._song(songs[-1])
            self.pump(.3)
        rows = [r for r in page.results.winfo_children() if isinstance(r, DiscoveryRow)]
        self.assertEqual(len(rows), 1)
        with patch.object(app._audio_preview, 'play', return_value=999) as play, patch.object(app._audio_preview, 'stop'):
            rows[0].preview_btn.invoke()
            play.assert_called_once_with(suggestion.preview_url, direct=True)
            rows[0].preview_btn.invoke()
        with patch('ytdlp_app.discovery.find_similar', return_value=[]) as related:
            rows[0].similar_btn.invoke()
            self.pump(.3)
            self.assertEqual(related.call_args.args[0], '34')
        report = recommendations.RecommendationResult((recommendations.Suggestion(suggestion, ('Artist — Song 0',)),), 8, 8, ())
        with patch.object(recommendations, 'for_playlist', return_value=report) as recommend:
            page.recommend_btn.invoke()
            self.pump(.3)
            self.assertEqual(len(recommend.call_args.args[0]), 60)
        self.assertIn('1 suggestions', page.status.cget('text'))
        app.tabs.set('Music')

    def test_playlist_sidebar_expands_selects_and_collapses(self):
        app = self.app
        tabs = app.tabs
        selected = MagicMock()
        tabs.set_playlist_items([('a', 'Same name'), ('b', 'Same name'), ('c', 'A very long playlist name to truncate')] + [(f'extra{i}', f'Extra {i}') for i in range(20)], selected)
        tabs.set('Music')
        original_geometry = app.geometry()
        app.geometry('1080x720')
        with patch.object(tabs, 'playlist_open_command'):
            tabs.buttons['Playlists'].invoke()
            self.pump()
            self.assertTrue(tabs.playlists_expanded)
            self.assertTrue(tabs.playlist_children.winfo_ismapped())
            self.assertTrue(tabs.buttons['Settings'].winfo_ismapped())
            self.assertTrue(tabs.buttons['recent'].winfo_ismapped())
            canvas = tabs.playlist_children._parent_canvas
            canvas.yview_moveto(0)
            if int(str(app.tk.call('info', 'patchlevel')).split('.')[0]) >= 9:
                tabs.playlist_buttons['a']._text_label.event_generate('<TouchpadScroll>', delta=24 * app._scroll_sign)
            else:
                tabs.playlist_buttons['a']._text_label.event_generate('<MouseWheel>', delta=-120 * app._scroll_sign)
            self.pump()
            self.assertGreater(canvas.yview()[0], 0)
            tabs.playlist_buttons['b'].invoke()
            selected.assert_called_once_with('b')
            tabs.set_playlist_selection('b')
            self.assertEqual(tabs._playlist_selected, 'b')
            tabs.buttons['Playlists'].invoke()
            self.pump()
            self.assertFalse(tabs.playlist_children.winfo_ismapped())
        app.geometry(original_geometry)
        page = app.playlists_page
        tabs.set_playlist_items([(key, p.name) for key, p in page._playlists.items()], page._select)
        tabs.set('Music')

    def test_playlist_artwork_is_applied_on_ui_thread(self):
        from ytdlp_app import playlists
        page = self.app.playlists_page
        page._tracks = [playlists.PlaylistTrack('art', 'Song', 'Artist')]
        callbacks = []
        with patch.object(page._artwork, 'load', side_effect=lambda track, folders, generation, callback: callbacks.append(callback)):
            page._filter_songs()
        self.assertEqual(len(callbacks), 1)
        callbacks[0](Image.new('RGB', (48, 48), 'red'))
        self.pump(.2)
        self.assertTrue(page._art_events.empty())
        # A delayed image from a destroyed row must not touch the replacement.
        page._tracks = []
        page._filter_songs()
        callbacks[0](Image.new('RGB', (48, 48), 'blue'))
        self.pump(.2)
        self.assertTrue(page._art_events.empty())

    def test_playlist_filter_columns_focus_and_scroll(self):
        from ytdlp_app.playlists import PlaylistTrack
        app, page = self.app, self.app.playlists_page
        app.tabs.set('Playlists')
        page.views.set('Songs')
        page._tracks = [PlaylistTrack(str(i), f'Été {i}', 'Singer', 'Album', 201) for i in range(70)]
        page.song_filter.set('')
        page._filter_songs()
        self.pump(.3)
        canvas = page.songs._parent_canvas
        self.assertIn(canvas, app._scroll_canvases)
        self.assertLess(canvas.yview()[1], 1)
        major = int(str(app.tk.call('info', 'patchlevel')).split('.')[0])
        if major >= 9:
            page.songs.winfo_children()[0].cells['Song']._label.event_generate('<TouchpadScroll>', delta=24 * app._scroll_sign)
        else:
            page.songs.winfo_children()[0].cells['Song']._label.event_generate('<MouseWheel>', delta=-120 * app._scroll_sign)
        self.pump(.1)
        self.assertGreater(canvas.yview()[0], 0)
        canvas.yview_moveto(.5)
        page.filter_entry.insert(0, 'singer ete 69')
        self.pump(.2)
        self.assertEqual([t.library_id for t in page._filtered_tracks], ['69'])
        row = page.songs.winfo_children()[0]
        self.assertEqual(row.cells['Album'].cget('text'), 'Album')
        self.assertEqual(row.cells['Time'].cget('text'), '3:21')
        self.assertEqual(canvas.yview()[0], 0)
        app._focus_search()
        self.assertEqual(app.tabs.get(), 'Playlists')
        self.assertEqual(page.views.get(), 'Songs')
        for frame in (page.results, app.identify_page.results, app.tabs.playlist_children):
            self.assertIn(frame._parent_canvas, app._scroll_canvases)
        page.song_filter.set('')
        self.pump(.2)

    def test_discovery_download_preserves_metadata_and_requires_review(self):
        from ytdlp_app.discovery import DiscoveredTrack
        from ytdlp_app.discovery_widgets import DiscoveryRow
        app = self.app
        track = DiscoveredTrack('123', 'A song', 'An artist', '', album='Record', duration_s=201,
                                genres=('Pop',), release_date='2020-01-01', track_number=3)
        row = DiscoveryRow(app.playlists_page.results, app, track, lambda t: None)
        self.assertEqual(row.cells['Album'].cget('text'), 'Record')
        self.assertEqual(row.preview_btn.cget('text'), '▶')
        self.assertEqual(row.preview_btn.cget('state'), 'disabled')
        with patch.object(app.jobs, 'enqueue', return_value=MagicMock(id='test')) as enqueue:
            row.download_btn.invoke()
        self.assertEqual(app.tabs.get(), 'Music')
        self.assertEqual(app.music_tracks[0].album, 'Record')
        self.assertEqual(app.music_tracks[0].track_number, 3)
        self.assertFalse(app._music_auto_download)
        self.assertEqual(enqueue.call_args.kwargs['kind'], 'source_match_all')
        row.destroy()

    def test_discovery_artwork_resizes_and_incremental_render_cancels(self):
        from ytdlp_app.discovery import DiscoveredTrack
        from ytdlp_app.discovery_widgets import DiscoveryRow, DiscoveryRenderer
        page = self.app.playlists_page
        page._clear(page.results)
        track = DiscoveredTrack('1', 'Song', 'Artist', '', artwork_url='https://example.test/cover')
        callbacks = []
        with patch('ytdlp_app.thumbcache.load', side_effect=lambda url, callback: callbacks.append(callback)):
            row = DiscoveryRow(page.results, self.app, track, lambda _: None)
        callbacks[0](Image.new('RGB', (100, 100), 'red'))
        self.pump(.2)
        self.assertEqual(row._image.cget('light_image').size, (48, 48))
        self.assertEqual(row._image.cget('dark_image').size, (48, 48))
        self.assertEqual(row._image.cget('light_image').getpixel((24, 24)), (255, 0, 0))
        row.destroy()
        renderer = DiscoveryRenderer(page.results, self.app, lambda _: None)
        plain = DiscoveredTrack('2', 'Other', 'Artist', '')
        renderer.show([(plain, '')] * 20)
        self.assertEqual(len(page.results.winfo_children()), 2)
        renderer.cancel()
        self.pump(.1)
        self.assertEqual(len(page.results.winfo_children()), 2)
        page._clear(page.results)


if __name__ == '__main__':
    unittest.main()
