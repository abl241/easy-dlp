"""Small, cached local covers. No network requests or recursive folder scans."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import threading
import time

import mutagen
from PIL import Image, ImageOps

SIZE = (48, 48)
_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.JPG', '.JPEG', '.PNG')


def _image(source):
    try:
        with Image.open(source) as image:
            image.thumbnail((192, 192))
            return ImageOps.pad(image.convert('RGB'), SIZE, color='#232629')
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _embedded(path):
    try:
        audio = mutagen.File(path)
        if audio is None:
            return None
        pictures = list(getattr(audio, 'pictures', []))
        tags = audio.tags
        if tags:
            if hasattr(tags, 'getall'):
                pictures.extend(tags.getall('APIC'))
            pictures.extend(tags.get('covr', []))
        for picture in pictures:
            image = _image(io.BytesIO(getattr(picture, 'data', picture)))
            if image is not None:
                return image
    except Exception:
        # Bad/unsupported audio tags must never prevent browsing the playlist.
        pass
    return None


def _safe_name(value):
    return str(value).replace('/', '_').replace('\\', '_').strip()


def find_artwork(track, folders=()):
    """Prefer exact sidecars, embedded tags, then shared album-folder covers."""
    audio = Path(track.audio_path).expanduser() if track.audio_path else None
    names = []
    if audio:
        names.append(audio.stem)
    if track.artist:
        names.extend((f'{track.artist} - {track.title}', f'{track.artist} — {track.title}'))
    names.append(track.title)
    names = list(dict.fromkeys(_safe_name(name) for name in names if name))
    roots = ([audio.parent] if audio else []) + [Path(folder).expanduser() for folder in folders if folder]
    seen = set()
    for root in roots:
        for name in names:
            for extension in _EXTENSIONS:
                path = root / (name + extension)
                if path in seen:
                    continue
                seen.add(path)
                if path.is_file():
                    image = _image(path)
                    if image is not None:
                        return image
    if audio and audio.is_file():
        image = _embedded(audio)
        if image is not None:
            return image
        # Only use generic covers beside the actual song, never from a
        # shared thumbnail directory containing unrelated albums.
        for name in ('cover', 'Cover', 'folder', 'Folder', 'front', 'Front'):
            for extension in _EXTENSIONS:
                path = audio.parent / (name + extension)
                if path.is_file():
                    image = _image(path)
                    if image is not None:
                        return image
    return None


class ArtworkLoader:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='playlist-art')
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False

    def new_view(self):
        with self._lock:
            self._generation += 1
            return self._generation

    def load(self, track, folders, generation, callback):
        key = (track.audio_path, track.title, track.artist, tuple(folders))
        def work():
            with self._lock:
                if self._closed or generation != self._generation:
                    return
                cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                image = cached[1]
            else:
                try:
                    image = find_artwork(track, folders)
                except OSError:
                    image = None
                with self._lock:
                    self._cache[key] = (time.monotonic() + 60, image)
                    self._cache.move_to_end(key)
                    while len(self._cache) > 256:
                        self._cache.popitem(last=False)
            with self._lock:
                current = not self._closed and generation == self._generation
            if current:
                callback(image)
        if not self._closed:
            self._pool.submit(work)

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
