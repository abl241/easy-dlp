"""On-demand, session-cached 30-second previews of the selected media."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading

import yt_dlp

from .downloader import _shared_opts
from .runtime import find_ffmpeg


def _safe_error(message):
    """Keep signed stream URLs and request credentials out of UI diagnostics."""
    message = re.sub(r"https?://\S+", "[media URL]", message)
    message = re.sub(r"(?im)^.*(?:cookie|authorization).*?$", "[authentication details omitted]", message)
    return " ".join(message.split())[:240] or "Audio processing failed."


class AudioPreview:
    def __init__(self):
        self.events = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")
        self._temp = tempfile.TemporaryDirectory(prefix="easy-dlp-preview-")
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._process = None
        self._generation = 0
        self._closed = False

    def stop(self):
        with self._lock:
            self._generation += 1
            self._cancel.set()
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            return self._generation

    def play(self, url, cookies=None, *, direct=False, catalog_track=None):
        self.stop()
        with self._lock:
            if self._closed:
                return self._generation
            token = self._generation
            cancel = self._cancel = threading.Event()
        self._pool.submit(self._run, token, cancel, url, cookies, direct, catalog_track)
        return token

    def _command(self, args, cancel, timeout):
        errors = tempfile.TemporaryFile()
        with self._lock:
            if cancel.is_set():
                errors.close()
                return False
            try:
                process = self._process = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=errors,
                )
            except Exception:
                errors.close()
                raise
        try:
            process.wait(timeout=timeout)
            if cancel.is_set():
                return False
            if process.returncode:
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", errors="replace")
                if "403" in detail:
                    raise RuntimeError("The audio server rejected the stream (HTTP 403).")
                raise RuntimeError("Audio tool failed: " + _safe_error(detail))
            return True
        except subprocess.TimeoutExpired:
            raise RuntimeError("Audio processing timed out. Try the preview again.") from None
        finally:
            errors.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            with self._lock:
                if self._process is process:
                    self._process = None

    def _run(self, token, cancel, url, cookies, direct=False, catalog_track=None):
        try:
            if cancel.is_set():
                return
            if catalog_track is not None:
                from .recommendations import _catalog_candidates
                self.events.put((token, 'loading', 'Looking for a matching Apple catalog sample…'))
                candidates = _catalog_candidates(catalog_track)
                sample = next((row.get('previewUrl') for row in candidates if row.get('previewUrl')), None)
                if not sample:
                    raise RuntimeError('No reliable catalog sample found. Right-click the play button and choose Play selected YouTube source.')
                url, direct = sample, True
                if cancel.is_set():
                    return
            player = shutil.which("afplay" if sys.platform == "darwin" else "ffplay")
            if not player:
                raise RuntimeError("Audio playback is unavailable. Install ffplay (part of FFmpeg).")
            path = Path(self._temp.name) / (hashlib.sha256(url.encode()).hexdigest() + ".wav")
            if not path.exists():
                ffmpeg = find_ffmpeg()
                if not ffmpeg:
                    raise RuntimeError("FFmpeg is required for audio previews.")
                opts = _shared_opts(self._temp.name, cookies)
                opts.update({"format": "bestaudio/best", "quiet": True, "no_warnings": True,
                             "ignoreerrors": False, "noplaylist": True,
                             "socket_timeout": 15, "retries": 1})
                if direct:
                    info = {"url": url}
                else:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                if cancel.is_set():
                    return
                headers = "".join(f"{k}: {v}\r\n" for k, v in info.get("http_headers", {}).items())
                partial = path.with_suffix(".part.wav")
                try:
                    args = [str(ffmpeg), "-nostdin", "-y", "-loglevel", "error", "-rw_timeout", "15000000"]
                    if headers:
                        args += ["-headers", headers]
                    args += ["-i", info["url"], "-t", "30", "-vn", "-ac", "2", "-ar", "44100", str(partial)]
                    try:
                        converted = self._command(args, cancel, 90)
                    except (RuntimeError, subprocess.TimeoutExpired):
                        if direct:
                            raise
                        if cancel.is_set():
                            return
                        self.events.put((token, "loading", "Fetching audio through yt-dlp for this preview…"))
                        # Some YouTube streams reject FFmpeg's HTTP requests.
                        # yt-dlp handles their cookies, chunking and retries.
                        with tempfile.TemporaryDirectory(dir=self._temp.name) as download_dir:
                            def check_cancel(_progress):
                                if cancel.is_set():
                                    raise RuntimeError("Preview cancelled")
                            fallback = dict(opts)
                            fallback.update(outtmpl=str(Path(download_dir) / "audio.%(ext)s"),
                                            progress_hooks=[check_cancel], noprogress=True)
                            with yt_dlp.YoutubeDL(fallback) as ydl:
                                downloaded = ydl.extract_info(url, download=True)
                                source = ydl.prepare_filename(downloaded)
                            if cancel.is_set():
                                return
                            converted = self._command(
                                [str(ffmpeg), "-nostdin", "-y", "-loglevel", "error",
                                 "-i", source, "-t", "30", "-vn", "-ac", "2",
                                 "-ar", "44100", str(partial)], cancel, 90,
                            )
                    if not converted:
                        return
                    partial.replace(path)
                finally:
                    partial.unlink(missing_ok=True)
            if cancel.is_set():
                return
            self.events.put((token, "playing", "Playing catalog preview" if direct else "Playing first 30 seconds of selected match"))
            args = [player, str(path)] if sys.platform == "darwin" else [player, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
            if self._command(args, cancel, 45):
                self.events.put((token, "done", "Preview finished"))
        except Exception as error:
            if not cancel.is_set():
                self.events.put((token, "error", "Preview unavailable: " + _safe_error(str(error))))

    def close(self):
        self.stop()
        self._closed = True
        # Cleanup follows the worker so it cannot recreate files after cleanup.
        self._pool.submit(self._temp.cleanup)
        self._pool.shutdown(wait=False, cancel_futures=False)
