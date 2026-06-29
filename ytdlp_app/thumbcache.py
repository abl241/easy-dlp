"""Background thumbnail loader with an in-memory LRU cache.

Used by `_ResultRow` to populate per-row thumbnails without blocking the Tk
event loop. Network I/O happens on a small ThreadPoolExecutor; the caller
provides a callback that is invoked from the worker thread with the decoded
PIL Image (or None on failure). The caller is responsible for marshalling
back to the UI thread via ``widget.after(0, ...)``.

Design notes:
- We cache the *decoded* full-size image (not a resized version) so that
  callers needing different sizes get cheap resizes without redownloading.
- Cache is a bounded OrderedDict acting as a basic LRU.
- A single shared executor is created lazily so that importing this module
  in the GUI process doesn't immediately spawn threads.
"""

from __future__ import annotations

import io
import logging
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PIL import Image, ImageDraw

_LOG = logging.getLogger(__name__)

_MAX_CACHE = 256
_MAX_WORKERS = 4
_FETCH_TIMEOUT_S = 8
_USER_AGENT = "Mozilla/5.0 (easy-dlp thumbnail fetcher)"

OnLoaded = Callable[[Optional[Image.Image]], None]

_cache: "OrderedDict[str, Image.Image]" = OrderedDict()
_cache_lock = threading.Lock()
_inflight: dict[str, list[OnLoaded]] = {}
_inflight_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="thumb"
            )
        return _executor


def _cache_get(url: str) -> Image.Image | None:
    with _cache_lock:
        img = _cache.get(url)
        if img is not None:
            # Touch for LRU.
            _cache.move_to_end(url)
        return img


def _cache_put(url: str, img: Image.Image) -> None:
    with _cache_lock:
        _cache[url] = img
        _cache.move_to_end(url)
        while len(_cache) > _MAX_CACHE:
            _cache.popitem(last=False)


def _fetch(url: str) -> Image.Image | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data))
        # Force load+decode now so callers (often in the UI thread) don't pay
        # the cost when they resize. Convert to RGB to drop palette modes
        # that some YouTube webp variants use.
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        return img
    except (urllib.error.URLError, OSError, ValueError) as e:
        _LOG.debug("Thumbnail fetch failed for %s: %s", url, e)
        return None


def _run_callbacks(url: str, img: Image.Image | None) -> None:
    with _inflight_lock:
        callbacks = _inflight.pop(url, [])
    for cb in callbacks:
        try:
            cb(img)
        except Exception:  # noqa: BLE001
            _LOG.exception("Thumbnail callback raised for %s", url)


def _worker(url: str) -> None:
    img = _fetch(url)
    if img is not None:
        _cache_put(url, img)
    _run_callbacks(url, img)


def load(url: str | None, on_loaded: OnLoaded) -> None:
    """Look up `url` in the cache; if absent, fetch in background.

    `on_loaded(img_or_none)` is always invoked exactly once. It may run on the
    calling thread (cache hit / empty URL) or on a background worker thread.
    The caller MUST marshal any UI updates back to the Tk main thread.
    """
    if not url:
        on_loaded(None)
        return
    cached = _cache_get(url)
    if cached is not None:
        on_loaded(cached)
        return
    with _inflight_lock:
        existing = _inflight.get(url)
        if existing is not None:
            existing.append(on_loaded)
            return
        _inflight[url] = [on_loaded]
    _get_executor().submit(_worker, url)


def placeholder(size: tuple[int, int]) -> Image.Image:
    """Create a flat gray placeholder image of the given size."""
    w, h = size
    img = Image.new("RGB", (w, h), color=(40, 44, 52))
    draw = ImageDraw.Draw(img)
    # Diagonal stripe so it doesn't look like a loaded image with no content.
    draw.line([(0, h), (w, 0)], fill=(70, 74, 82), width=2)
    return img


def shutdown() -> None:
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None
