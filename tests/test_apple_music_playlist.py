import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ytdlp_app import apple_music as am


class PlaylistImportTests(unittest.TestCase):
    def test_playlist_id_and_path_are_arguments_not_script_interpolation(self):
        with patch.object(am.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = am._import_via_playlist(Path('/tmp/A "quoted" song.mp3'), 'ID123')
        self.assertTrue(result.success)
        args = run.call_args.args[0]
        self.assertEqual(args[-2:], [str(Path('/tmp/A "quoted" song.mp3').resolve()), 'ID123'])
        self.assertNotIn('ID123', args[2])

    def test_playlist_failure_never_falls_back_to_library_or_removes_file(self):
        with tempfile.NamedTemporaryFile() as source, \
             patch.object(am, 'is_supported', return_value=True), \
             patch.object(am, '_import_via_playlist', return_value=am.ImportResult(False, 'missing playlist')), \
             patch.object(am, '_import_via_auto_add_folder') as fallback:
            result = am.import_to_library(source.name, playlist_id='ID', remove_source=True)
            self.assertFalse(result.success)
            self.assertTrue(Path(source.name).exists())
            fallback.assert_not_called()

    def test_playlist_success_keeps_source_even_if_music_uses_original_location(self):
        with tempfile.NamedTemporaryFile() as source, \
             patch.object(am, 'is_supported', return_value=True), \
             patch.object(am, '_import_via_playlist', return_value=am.ImportResult(True)):
            self.assertTrue(am.import_to_library(source.name, playlist_id='ID', remove_source=True).success)
            self.assertTrue(Path(source.name).exists())
