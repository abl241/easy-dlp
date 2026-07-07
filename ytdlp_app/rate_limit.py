"""YouTube rate-limit detection, backoff, and yt-dlp call wrapping."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, TypeVar

import yt_dlp

ProgressFn = Callable[[str], None]
T = TypeVar("T")

_RATE_LIMIT_RE = re.compile(
    r"rate[- ]?limit|429|try again later|exceeded.*quota",
    re.IGNORECASE,
)

_INITIAL_BACKOFF_S = 30.0
_MAX_BACKOFF_S = 600.0
_BACKOFF_MULTIPLIER = 2.0

_sleep_interval_requests: float | None = None


def set_sleep_interval_requests(seconds: float | None) -> None:
    global _sleep_interval_requests
    _sleep_interval_requests = seconds


def get_sleep_interval_requests() -> float | None:
    return _sleep_interval_requests


class RateLimitGuard:
    """Thread-safe global backoff state for YouTube extraction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backoff_until = 0.0
        self._current_backoff = _INITIAL_BACKOFF_S

    def is_rate_limited_error(self, message: str) -> bool:
        return bool(message and _RATE_LIMIT_RE.search(message))

    def wait_if_needed(self, progress: ProgressFn = lambda msg: None) -> None:
        with self._lock:
            wait_until = self._backoff_until
        remaining = wait_until - time.monotonic()
        if remaining <= 0:
            return
        secs = int(remaining) + 1
        progress(f"WARN: YouTube rate limit — waiting {secs}s…")
        while remaining > 0:
            time.sleep(min(5.0, remaining) if remaining > 5 else remaining)
            remaining = wait_until - time.monotonic()

    def note_success(self) -> None:
        with self._lock:
            self._current_backoff = _INITIAL_BACKOFF_S

    def note_rate_limit(self, progress: ProgressFn = lambda msg: None) -> float:
        with self._lock:
            wait_s = self._current_backoff
            self._backoff_until = time.monotonic() + wait_s
            self._current_backoff = min(
                _MAX_BACKOFF_S, self._current_backoff * _BACKOFF_MULTIPLIER,
            )
        progress(f"WARN: YouTube rate limit hit — backing off {int(wait_s)}s")
        return wait_s


_guard = RateLimitGuard()


def guard_ytdlp_call(
    fn: Callable[[], T],
    *,
    progress: ProgressFn = lambda msg: None,
    max_retries: int = 2,
) -> T:
    """Run a yt-dlp operation with proactive wait and retry on rate limits."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        _guard.wait_if_needed(progress)
        try:
            result = fn()
            _guard.note_success()
            return result
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            if _guard.is_rate_limited_error(str(e)) and attempt < max_retries:
                _guard.note_rate_limit(progress)
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _guard.is_rate_limited_error(str(e)) and attempt < max_retries:
                _guard.note_rate_limit(progress)
                continue
            raise
    if last_err is not None:
        raise last_err
    raise RuntimeError("guard_ytdlp_call failed without exception")


def apply_rate_limit_opts(opts: dict[str, Any]) -> None:
    interval = get_sleep_interval_requests()
    if interval is not None and interval > 0:
        opts["sleep_interval_requests"] = interval
