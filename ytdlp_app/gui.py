"""customtkinter front-end."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from . import __version__
from .downloader import (
    DownloadResult,
    download_audio,
    download_thumbnails_only,
    download_video,
    parse_urls,
)
from .embed import embed_folder, embed_single
from .settings import Settings


ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


# ----------------------------- worker plumbing ---------------------------- #

@dataclass
class _Message:
    """A message posted from a worker thread to the UI."""
    kind: str          # "log" | "done"
    text: str = ""
    success: bool = True


class JobRunner:
    """Runs one job at a time on a daemon thread, posts log lines + completion
    events to a queue that the Tk main loop polls."""

    def __init__(self, root: ctk.CTk) -> None:
        self._root = root
        self._queue: queue.Queue[_Message] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[_Message], None]] = []
        self._poll()

    def add_listener(self, fn: Callable[[_Message], None]) -> None:
        self._listeners.append(fn)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, target: Callable[[Callable[[str], None]], None]) -> bool:
        """Run target(progress_fn) on a background thread. Returns False if a
        job is already running."""
        if self.is_running():
            return False

        def progress(msg: str) -> None:
            self._queue.put(_Message(kind="log", text=msg))

        def runner() -> None:
            try:
                target(progress)
            except Exception as e:  # noqa: BLE001
                self._queue.put(_Message(kind="log", text=f"FATAL: {e}", success=False))
                self._queue.put(_Message(kind="done", success=False))
            else:
                self._queue.put(_Message(kind="done", success=True))

        self._thread = threading.Thread(target=runner, name="ytdlp-job", daemon=True)
        self._thread.start()
        return True

    def _poll(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                for listener in self._listeners:
                    listener(msg)
        except queue.Empty:
            pass
        self._root.after(100, self._poll)


# ------------------------------ UI helpers -------------------------------- #

def _pick_folder(initial: str = "") -> str:
    chosen = filedialog.askdirectory(initialdir=initial or str(Path.home()))
    return chosen or ""


def _pick_file(initial: str = "", types: list[tuple[str, str]] | None = None) -> str:
    chosen = filedialog.askopenfilename(
        initialdir=initial or str(Path.home()),
        filetypes=types or [("All files", "*.*")],
    )
    return chosen or ""


def _row_folder(parent, label: str, value: str, on_change: Callable[[str], None]):
    """A row with a label, an editable directory path, a "Browse..." button."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", padx=10, pady=4)
    ctk.CTkLabel(frame, text=label, width=120, anchor="w").pack(side="left")
    var = ctk.StringVar(value=value)
    entry = ctk.CTkEntry(frame, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def browse() -> None:
        chosen = _pick_folder(var.get())
        if chosen:
            var.set(chosen)
            on_change(chosen)

    def commit(_event=None) -> None:
        on_change(var.get())

    entry.bind("<FocusOut>", commit)
    entry.bind("<Return>", commit)
    ctk.CTkButton(frame, text="Browse...", width=90, command=browse).pack(side="left")
    return var


def _row_file(parent, label: str, value: str, on_change: Callable[[str], None],
              types: list[tuple[str, str]] | None = None):
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", padx=10, pady=4)
    ctk.CTkLabel(frame, text=label, width=120, anchor="w").pack(side="left")
    var = ctk.StringVar(value=value)
    entry = ctk.CTkEntry(frame, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def browse() -> None:
        chosen = _pick_file(var.get(), types=types)
        if chosen:
            var.set(chosen)
            on_change(chosen)

    def commit(_event=None) -> None:
        on_change(var.get())

    entry.bind("<FocusOut>", commit)
    entry.bind("<Return>", commit)
    ctk.CTkButton(frame, text="Browse...", width=90, command=browse).pack(side="left")
    return var


# ------------------------------ main window ------------------------------- #

class App(ctk.CTk):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.title(f"YTDLP {__version__}")
        self.geometry("820x640")
        self.minsize(700, 520)

        self.settings = settings
        self.runner = JobRunner(self)
        self.runner.add_listener(self._on_message)

        # ---- shared status bar at the bottom -------------------------- #
        status_frame = ctk.CTkFrame(self)
        status_frame.pack(side="bottom", fill="x")
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(status_frame, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=10, pady=6,
        )
        self.progress = ctk.CTkProgressBar(status_frame, mode="indeterminate", width=140)
        self.progress.pack(side="right", padx=10, pady=6)
        self.progress.set(0)

        # ---- log pane (collapsed below tabs) -------------------------- #
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(side="bottom", fill="both", expand=False, padx=10, pady=(0, 6))
        ctk.CTkLabel(log_frame, text="Log", anchor="w").pack(fill="x", padx=4)
        self.log_box = ctk.CTkTextbox(log_frame, height=160, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.log_box.configure(state="disabled")

        # ---- cookies row at the top ----------------------------------- #
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(side="top", fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(top, text="Cookies file (optional):", width=160, anchor="w").pack(side="left")
        cookies_var = ctk.StringVar(value=self.settings.get("cookies_path"))
        self._cookies_var = cookies_var
        cookies_entry = ctk.CTkEntry(top, textvariable=cookies_var)
        cookies_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def commit_cookies(_event=None) -> None:
            self.settings.set("cookies_path", cookies_var.get())
        cookies_entry.bind("<FocusOut>", commit_cookies)
        cookies_entry.bind("<Return>", commit_cookies)

        def browse_cookies() -> None:
            chosen = _pick_file(cookies_var.get(),
                                types=[("Cookies (Netscape)", "*.txt"), ("All files", "*.*")])
            if chosen:
                cookies_var.set(chosen)
                self.settings.set("cookies_path", chosen)
        ctk.CTkButton(top, text="Browse...", width=90, command=browse_cookies).pack(side="left")

        # ---- tabs ----------------------------------------------------- #
        tabs = ctk.CTkTabview(self)
        tabs.pack(side="top", fill="both", expand=True, padx=10, pady=10)

        self._build_audio_tab(tabs.add("Audio (MP3)"))
        self._build_video_tab(tabs.add("Video (MP4)"))
        self._build_thumbs_tab(tabs.add("Thumbnails"))
        self._build_embed_tab(tabs.add("Embed Thumbnail"))

    # ------------------------------ logging ------------------------------ #

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_message(self, msg: _Message) -> None:
        if msg.kind == "log":
            self._log(msg.text)
            self.status_var.set(msg.text[:100])
        elif msg.kind == "done":
            self.progress.stop()
            self.progress.set(0)
            self.status_var.set("Done." if msg.success else "Finished with errors. See log.")
            self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._action_buttons:
            btn.configure(state="normal" if enabled else "disabled")

    # ------------------------------ tabs --------------------------------- #

    def __post_init_buttons(self) -> None:
        if not hasattr(self, "_action_buttons"):
            self._action_buttons: list[ctk.CTkButton] = []

    def _register_button(self, btn: ctk.CTkButton) -> None:
        self.__post_init_buttons()
        self._action_buttons.append(btn)

    def _start_job(self, target):
        if not self.runner.submit(target):
            self.status_var.set("A download is already running.")
            return
        self._set_buttons_enabled(False)
        self.progress.start()
        self.status_var.set("Running...")

    def _build_audio_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Download audio as MP3 with embedded thumbnail.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 4))

        ctk.CTkLabel(parent, text="YouTube URL(s) — one per line:", anchor="w").pack(
            fill="x", padx=10
        )
        url_box = ctk.CTkTextbox(parent, height=80)
        url_box.pack(fill="x", padx=10, pady=4)
        url_box.insert("1.0", self.settings.get("audio_url"))

        out_var = _row_folder(
            parent, "Output dir:",
            self.settings.get("audio_dir"),
            lambda v: self.settings.set("audio_dir", v),
        )

        def go():
            text = url_box.get("1.0", "end").strip()
            self.settings.set("audio_url", text)
            urls = parse_urls(text)
            if not urls:
                self.status_var.set("Enter at least one URL.")
                return
            out_dir = out_var.get()
            cookies = self._cookies_var.get()
            self._start_job(lambda p: self._wrap_result(
                "Audio",
                download_audio(urls, out_dir, cookies_path=cookies or None, progress=p),
                p,
            ))

        btn = ctk.CTkButton(parent, text="Download Audio", command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._register_button(btn)

    def _build_video_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Download video as MP4 (H.264/AAC where possible) with embedded thumbnail.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 4))

        ctk.CTkLabel(parent, text="YouTube URL(s) — one per line:", anchor="w").pack(
            fill="x", padx=10
        )
        url_box = ctk.CTkTextbox(parent, height=80)
        url_box.pack(fill="x", padx=10, pady=4)
        url_box.insert("1.0", self.settings.get("video_url"))

        out_var = _row_folder(
            parent, "Output dir:",
            self.settings.get("video_dir"),
            lambda v: self.settings.set("video_dir", v),
        )

        def go():
            text = url_box.get("1.0", "end").strip()
            self.settings.set("video_url", text)
            urls = parse_urls(text)
            if not urls:
                self.status_var.set("Enter at least one URL.")
                return
            out_dir = out_var.get()
            cookies = self._cookies_var.get()
            self._start_job(lambda p: self._wrap_result(
                "Video",
                download_video(urls, out_dir, cookies_path=cookies or None, progress=p),
                p,
            ))

        btn = ctk.CTkButton(parent, text="Download Video", command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._register_button(btn)

    def _build_thumbs_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Download thumbnails only (converted to JPG). No audio or video.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 4))

        ctk.CTkLabel(parent, text="YouTube URL(s) — one per line:", anchor="w").pack(
            fill="x", padx=10
        )
        url_box = ctk.CTkTextbox(parent, height=80)
        url_box.pack(fill="x", padx=10, pady=4)
        url_box.insert("1.0", self.settings.get("thumb_url"))

        out_var = _row_folder(
            parent, "Output dir:",
            self.settings.get("thumb_dir"),
            lambda v: self.settings.set("thumb_dir", v),
        )

        def go():
            text = url_box.get("1.0", "end").strip()
            self.settings.set("thumb_url", text)
            urls = parse_urls(text)
            if not urls:
                self.status_var.set("Enter at least one URL.")
                return
            out_dir = out_var.get()
            cookies = self._cookies_var.get()
            self._start_job(lambda p: self._wrap_result(
                "Thumbnails",
                download_thumbnails_only(urls, out_dir, cookies_path=cookies or None, progress=p),
                p,
            ))

        btn = ctk.CTkButton(parent, text="Download Thumbnails", command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._register_button(btn)

    def _build_embed_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text=("Embed a new thumbnail into an existing MP3.\n"
                  "Single mode: pick a video file and a thumbnail file.\n"
                  "Folder mode: pick a folder of MP3s and a folder of thumbnails — "
                  "matched by filename (Song.mp3 + Song.jpg)."),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=10, pady=(8, 4))

        # Mode toggle
        mode_var = ctk.StringVar(value="folder")
        mode_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mode_frame.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(mode_frame, text="Mode:", width=120, anchor="w").pack(side="left")
        ctk.CTkRadioButton(mode_frame, text="Folder", variable=mode_var, value="folder").pack(side="left", padx=4)
        ctk.CTkRadioButton(mode_frame, text="Single file", variable=mode_var, value="single").pack(side="left", padx=4)

        video_var = _row_folder(
            parent, "Audio/video:",
            self.settings.get("embed_video_dir"),
            lambda v: self.settings.set("embed_video_dir", v),
        )
        thumb_var = _row_folder(
            parent, "Thumbnail(s):",
            self.settings.get("embed_thumb_dir"),
            lambda v: self.settings.set("embed_thumb_dir", v),
        )
        out_var = _row_folder(
            parent, "Output dir:",
            self.settings.get("embed_out_dir"),
            lambda v: self.settings.set("embed_out_dir", v),
        )

        ctk.CTkLabel(
            parent,
            text="*Output dir must be different than the audio dir.",
            anchor="w",
            text_color=("gray40", "gray70"),
        ).pack(fill="x", padx=10, pady=(2, 0))

        def go():
            mode = mode_var.get()
            video = video_var.get()
            thumb = thumb_var.get()
            out_dir = out_var.get()
            if not out_dir:
                self.status_var.set("Pick an output directory.")
                return

            if mode == "single":
                runner = lambda p: self._wrap_embed_result(
                    embed_single(video, thumb, out_dir, progress=p), p,
                )
            else:
                runner = lambda p: self._wrap_embed_result(
                    embed_folder(video, thumb, out_dir, progress=p), p,
                )
            self._start_job(runner)

        btn = ctk.CTkButton(parent, text="Embed", command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._register_button(btn)

    # ----------------------------- result wrappers ----------------------- #

    def _wrap_result(self, label: str, result: DownloadResult, progress) -> None:
        if result.success:
            progress(f"[{label}] SUCCESS")
        else:
            progress(f"[{label}] FAILED: {result.message or 'see log above'}")

    def _wrap_embed_result(self, result, progress) -> None:
        progress(f"[Embed] processed={result.processed} failed={result.failed} "
                 f"out={result.output_dir}")
