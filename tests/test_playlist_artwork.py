import io
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

from PIL import Image
from mutagen.wave import WAVE
from mutagen.id3 import APIC
from ytdlp_app.playlists import PlaylistTrack, local_path
from ytdlp_app.playlist_artwork import ArtworkLoader, find_artwork, SIZE


class ArtworkTests(unittest.TestCase):
    def test_exact_song_sidecar_and_folder_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Image.new('RGB', (100, 100), 'red').save(root / 'Artist - Song.jpg')
            image = find_artwork(PlaylistTrack('1', 'Song', 'Artist'), [folder])
            self.assertEqual(image.size, SIZE)
            self.assertGreater(image.getpixel((24, 24))[0], 240)
            self.assertIsNone(find_artwork(PlaylistTrack('2', 'Different', 'Artist'), [folder]))

    def test_embedded_art_is_read_from_actual_audio_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'song.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b'\x00\x00' * 800)
            data = io.BytesIO()
            Image.new('RGB', (64, 64), 'blue').save(data, format='PNG')
            audio = WAVE(path)
            audio.add_tags()
            audio.tags.add(APIC(mime='image/png', type=3, data=data.getvalue()))
            audio.save()
            before = path.read_bytes()
            image = find_artwork(PlaylistTrack('1', 'Song', 'Artist', audio_path=str(path)))
            self.assertEqual(image.getpixel((24, 24)), (0, 0, 255))
            self.assertEqual(path.read_bytes(), before)

    def test_generic_cover_is_only_used_next_to_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Image.new('RGB', (64, 64), 'green').save(root / 'cover.png')
            path = root / 'song.mp3'
            path.touch()
            self.assertIsNone(find_artwork(PlaylistTrack('1', 'Song', 'Artist'), [folder]))
            self.assertIsNotNone(find_artwork(PlaylistTrack('1', 'Song', 'Artist', audio_path=str(path))))

    def test_local_file_urls_are_decoded_without_fetching_remote_locations(self):
        self.assertEqual(local_path('file://localhost/tmp/My%20Song.mp3'), '/tmp/My Song.mp3')
        self.assertEqual(local_path('https://example.com/song.mp3'), '')
        self.assertEqual(local_path('file://other-host/song.mp3'), '')

    def test_cache_avoids_repeated_disk_reads_and_stale_views(self):
        loader = ArtworkLoader()
        track = PlaylistTrack('1', 'Song', 'Artist')
        done = threading.Event()
        try:
            with patch('ytdlp_app.playlist_artwork.find_artwork', return_value=Image.new('RGB', SIZE)) as find:
                generation = loader.new_view()
                for _ in range(2):
                    done.clear()
                    loader.load(track, (), generation, lambda image: done.set())
                    self.assertTrue(done.wait(2))
                self.assertEqual(find.call_count, 1)
                loader.new_view()
                loader.load(track, (), generation, lambda image: self.fail('Stale callback'))
        finally:
            loader.close()
            loader._pool.shutdown(wait=True)
