"""Link validation must not silently drop inputs or route to the wrong service."""
import unittest
from ytdlp_app.imports import parse_import_links


class ImportLinks(unittest.TestCase):
    def test_youtube_duplicates_and_blank_lines(self):
        url = 'https://www.youtube.com/watch?v=example'
        self.assertEqual(parse_import_links(f'\n {url} \n\n{url}', music=True), ([url], 'youtube'))

    def test_spotify_links(self):
        urls = ['https://open.spotify.com/track/one', 'https://open.spotify.com/album/two']
        self.assertEqual(parse_import_links('\n'.join(urls), music=True), (urls, 'spotify'))

    def test_mixed_services_are_not_sent_to_first_resolver(self):
        with self.assertRaisesRegex(ValueError, 'separately'):
            parse_import_links('https://youtu.be/example\nhttps://open.spotify.com/album/two', music=True)

    def test_invalid_line_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, 'Line 2'):
            parse_import_links('https://youtu.be/example\nArtist - Title', music=True)

    def test_empty_unknown_and_malformed(self):
        for text in ('', '   ', 'https://other.example/track', 'https://[invalid', 'https://youtu.be/a b'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_import_links(text, music=True)

    def test_video_retains_generic_http_support(self):
        self.assertEqual(parse_import_links('https://example.org/video'), (['https://example.org/video'], None))
