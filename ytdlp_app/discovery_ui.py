"""Retained Identify page; workers never touch Tk widgets."""
import queue
import threading
import tempfile
from pathlib import Path
from tkinter import filedialog
from urllib.parse import urlparse

import customtkinter as ctk

from . import discovery
from .preview import _safe_error
from .ui import MUTED


class IdentifyPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._events = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self._stop = threading.Event()
        self._recording = False
        self._closed = False
        self._track = None
        self._related_seed = None
        self.pack(fill="both", expand=True)
        ctk.CTkLabel(self, text="Identify music", font=ctk.CTkFont(size=28, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(self, text="Recognize a song with Shazam, then discover related music.", text_color=MUTED).pack(anchor="w", pady=(0, 12))
        self.mode = ctk.CTkSegmentedButton(self, values=["Audio file", "YouTube link", "Microphone", "Computer audio"], command=self._mode_changed)
        self.mode.set("Audio file")
        self.mode.pack(anchor="w")
        self.source = ctk.StringVar()
        input_row = self.input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.pack(fill="x", pady=8)
        self.entry = ctk.CTkEntry(input_row, textvariable=self.source, placeholder_text="Choose an audio file")
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse = ctk.CTkButton(input_row, text="Choose file…", width=110, command=self._choose)
        self.browse.pack(side="left", padx=(8, 0))
        self.note = ctk.CTkLabel(self, text="Recognition sends an audio fingerprint to Shazam. Your file and tags stay unchanged.", text_color=MUTED, wraplength=750, justify="left")
        self.note.pack(anchor="w")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", pady=8)
        self.identify_btn = ctk.CTkButton(actions, text="Identify song", command=self._identify)
        self.identify_btn.pack(side="left")
        self.similar_btn = ctk.CTkButton(actions, text="Find similar", command=self._similar, state="disabled")
        self.similar_btn.pack(side="left", padx=8)
        self.cancel_btn = ctk.CTkButton(actions, text="Cancel", command=self._cancel_work, state="disabled", width=80)
        self.cancel_btn.pack(side="left")
        self.status = ctk.CTkLabel(self, text="Choose audio to get started.", anchor="w", wraplength=750, justify="left")
        self.status.pack(fill="x", pady=4)
        self.match = ctk.CTkFrame(self, fg_color="transparent")
        self.match.pack(fill="x")
        from .song_table import header
        header(self, action_width=0)
        self.results = ctk.CTkScrollableFrame(self)
        self.results.pack(fill="both", expand=True, pady=8)
        from .discovery_widgets import DiscoveryRenderer
        self._renderer = DiscoveryRenderer(self.results, app, self._similar_track)
        self._poll_id = self.after(100, self._poll)

    def _mode_changed(self, mode):
        self.source.set("")
        live = mode in ("Microphone", "Computer audio")
        self.identify_btn.configure(text="Listen" if live else "Identify song")
        if live:
            self.input_row.pack_forget()
            self.note.configure(text=("Records up to 20 seconds from your default microphone." if mode == "Microphone" else "Records up to 20 seconds of computer audio on macOS 13+. macOS may ask for Screen & System Audio Recording permission; no video is saved.") + " Stop & identify uses the sample; Cancel discards it. The recording is deleted after recognition.")
            return
        self.input_row.pack(fill="x", pady=8, before=self.note)
        self.browse.configure(state="normal" if mode == "Audio file" else "disabled")
        self.entry.configure(placeholder_text="Choose an audio file" if mode == "Audio file" else "Paste a YouTube video link")
        self.note.configure(text="Recognition sends an audio fingerprint to Shazam. Your file and tags stay unchanged." if mode == "Audio file" else "The audio is downloaded temporarily for recognition, then deleted. Its fingerprint is sent to Shazam.")

    def _choose(self):
        path = filedialog.askopenfilename(title="Choose audio to identify", filetypes=[("Audio files", "*.mp3 *.m4a *.wav *.flac *.ogg *.webm *.aac"), ("All files", "*")])
        if path:
            self.source.set(path)

    def _cancel_work(self):
        self._cancel.set()
        self.status.configure(text="Cancelling… waiting for the current request to finish.")

    def _identify(self):
        if self._busy:
            if self._recording:
                self._stop.set()
                self.identify_btn.configure(state="disabled")
                self.status.configure(text="Stopping recording…")
            return
        if self.mode.get() in ("Microphone", "Computer audio"):
            source_mode = self.mode.get()
            button = getattr(self.app, "_preview_button", None)
            if button is not None:
                button._toggle()
            self._start("identify", lambda: self._capture(source_mode))
            return
        source = self.source.get().strip()
        if not source:
            self.status.configure(text="Choose a file or paste a YouTube link first.")
            return
        is_link = self.mode.get() == "YouTube link"
        cookies = self.app.settings.get("cookies_path") or None
        self._start("identify", lambda: discovery.identify(source, is_link=is_link, cookies=cookies, cancel=self._cancel))

    def _capture(self, source_mode):
        from .capture import record
        with tempfile.TemporaryDirectory(prefix="easy-dlp-listen-") as folder:
            path = Path(folder) / "recording.m4a"
            record(source_mode, path, stop=self._stop, cancel=self._cancel,
                   progress=lambda message: self._events.put(("progress", message, None)))
            if self._cancel.is_set():
                raise RuntimeError("Cancelled")
            self._events.put(("progress", "Recognizing recorded audio…", None))
            return discovery.identify(str(path), is_link=False, cookies=None, cancel=self._cancel)

    def _similar(self):
        if self._track:
            self._similar_track(self._track)

    def _similar_track(self, track):
        if self._busy:
            return
        self._related_seed = track
        self._start("similar", lambda: discovery.find_similar(track.key, self._cancel))

    def _start(self, kind, work):
        if self._busy:
            return
        self._busy = True
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._recording = False
        for widget in (self.identify_btn, self.similar_btn, self.browse, self.mode, self.entry):
            widget.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status.configure(text="Recognizing audio…" if kind == "identify" else "Finding related songs…")
        def run():
            try:
                result = work()
                self._events.put((kind, result, None))
            except Exception as error:
                self._events.put((kind, None, _safe_error(str(error)) if str(error) else "The service timed out. Please try again."))
        threading.Thread(target=run, daemon=True, name="shazam-discovery").start()

    def _open(self, url):
        if urlparse(url).scheme in ("http", "https"):
            self.app._open_media_link(url)

    def _row(self, parent, track):
        from .discovery_widgets import DiscoveryRow
        return DiscoveryRow(parent, self.app, track, self._similar_track)

    def _poll(self):
        if self._closed:
            return
        try:
            kind, result, error = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == "progress":
                self._recording = result.startswith("●")
                if not self._cancel.is_set() and not self._stop.is_set():
                    self.status.configure(text=result)
                elif not self._cancel.is_set() and not self._recording:
                    self.status.configure(text=result)
                self.identify_btn.configure(text="Stop & identify" if self._recording else "Listen", state="normal" if self._recording and not self._stop.is_set() and not self._cancel.is_set() else "disabled")
                self._poll_id = self.after(100, self._poll)
                return
            self._recording = False
            self.identify_btn.configure(text="Listen" if self.mode.get() in ("Microphone", "Computer audio") else "Identify song")
            self._busy = False
            for widget in (self.identify_btn, self.mode, self.entry):
                widget.configure(state="normal")
            self.browse.configure(state="normal" if self.mode.get() == "Audio file" else "disabled")
            self.cancel_btn.configure(state="disabled")
            if self._cancel.is_set():
                self.status.configure(text="Cancelled.")
            elif error:
                self.status.configure(text=error)
            elif kind == "identify":
                self._track = result
                self._renderer.cancel()
                for parent in (self.match, self.results):
                    for child in parent.winfo_children():
                        child.destroy()
                self._row(self.match, result)
                detail = f" Apple Music ID: {result.apple_id}." if result.apple_id else ""
                self.status.configure(text="Song recognized." + detail + " Check the release before using its metadata.")
            else:
                self._renderer.cancel()
                for child in self.results.winfo_children():
                    child.destroy()
                self._renderer.show([(track, '') for track in result])
                self.status.configure(text=f"{len(result)} related songs for {self._related_seed.title}." if result else "Shazam returned no related songs for this track.")
            self.similar_btn.configure(state="normal" if self._track and self._track.key else "disabled")
        self._poll_id = self.after(100, self._poll)

    def destroy(self):
        self._closed = True
        self._renderer.cancel()
        self._cancel.set()
        self.after_cancel(self._poll_id)
        super().destroy()
