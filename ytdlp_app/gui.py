"""customtkinter front-end."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
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
from .embed import EmbedResult, embed_folder, embed_single
from .runtime import find_ffmpeg
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


def _reveal_in_file_manager(path: str | Path) -> None:
    """Open the OS file manager at the given path."""
    p = Path(path)
    if not p.exists():
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(p)], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", str(p)], check=False)
    else:
        subprocess.run(["xdg-open", str(p)], check=False)


def _path_row(parent, label: str, value: str, on_change: Callable[[str], None],
              *, kind: str = "folder",
              file_types: list[tuple[str, str]] | None = None):
    """A label + path entry + Browse button. `kind` is "folder" or "file"."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", padx=10, pady=4)
    ctk.CTkLabel(frame, text=label, width=120, anchor="w").pack(side="left")
    var = ctk.StringVar(value=value)
    entry = ctk.CTkEntry(frame, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def browse() -> None:
        if kind == "file":
            chosen = _pick_file(var.get(), types=file_types)
        else:
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


# ------------------------------ main window ------------------------------- #

class App(ctk.CTk):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.title(f"YTDLP {__version__}")
        self.geometry("860x680")
        self.minsize(720, 540)

        self.settings = settings
        self._action_buttons: list[ctk.CTkButton] = []
        self._last_output_dir: str | None = None

        self.runner = JobRunner(self)
        self.runner.add_listener(self._on_message)

        self._build_status_bar()
        self._build_log_pane()
        self._build_cookies_row()
        self._build_tabs()

        self._ffmpeg_preflight()

    # ----------------------------- top-level frames ---------------------- #

    def _build_status_bar(self) -> None:
        status_frame = ctk.CTkFrame(self)
        status_frame.pack(side="bottom", fill="x")
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(status_frame, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=10, pady=6,
        )
        self.open_folder_btn = ctk.CTkButton(
            status_frame, text="Open output folder", width=160,
            command=self._open_last_output_dir, state="disabled",
        )
        self.open_folder_btn.pack(side="right", padx=4, pady=6)
        self.progress = ctk.CTkProgressBar(status_frame, mode="indeterminate", width=140)
        self.progress.pack(side="right", padx=10, pady=6)
        self.progress.set(0)

    def _build_log_pane(self) -> None:
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(side="bottom", fill="both", expand=False, padx=10, pady=(0, 6))
        header = ctk.CTkFrame(log_frame, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(4, 0))
        ctk.CTkLabel(header, text="Log", anchor="w").pack(side="left")
        ctk.CTkButton(header, text="Clear", width=60, command=self._clear_log).pack(side="right")
        self.log_box = ctk.CTkTextbox(log_frame, height=160, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        self.log_box.configure(state="disabled")

    def _build_cookies_row(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(side="top", fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(top, text="Cookies file (optional):", width=160, anchor="w").pack(side="left")
        self._cookies_var = ctk.StringVar(value=self.settings.get("cookies_path"))
        entry = ctk.CTkEntry(top, textvariable=self._cookies_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def commit(_event=None) -> None:
            self.settings.set("cookies_path", self._cookies_var.get())

        def browse() -> None:
            chosen = _pick_file(
                self._cookies_var.get(),
                types=[("Cookies (Netscape)", "*.txt"), ("All files", "*.*")],
            )
            if chosen:
                self._cookies_var.set(chosen)
                self.settings.set("cookies_path", chosen)

        entry.bind("<FocusOut>", commit)
        entry.bind("<Return>", commit)
        ctk.CTkButton(top, text="Browse...", width=90, command=browse).pack(side="left")

    def _build_tabs(self) -> None:
        tabs = ctk.CTkTabview(self)
        tabs.pack(side="top", fill="both", expand=True, padx=10, pady=10)

        self._build_url_tab(
            tabs.add("Audio (MP3)"),
            description="Download audio as MP3 with embedded thumbnail.",
            url_setting="audio_url",
            dir_setting="audio_dir",
            button_label="Download Audio",
            download_fn=download_audio,
            job_label="Audio",
        )
        self._build_url_tab(
            tabs.add("Video (MP4)"),
            description="Download video as MP4 (H.264/AAC where possible) with embedded thumbnail.",
            url_setting="video_url",
            dir_setting="video_dir",
            button_label="Download Video",
            download_fn=download_video,
            job_label="Video",
        )
        self._build_url_tab(
            tabs.add("Thumbnails"),
            description="Download thumbnails only (converted to JPG). No audio or video.",
            url_setting="thumb_url",
            dir_setting="thumb_dir",
            button_label="Download Thumbnails",
            download_fn=download_thumbnails_only,
            job_label="Thumbnails",
        )
        self._build_embed_tab(tabs.add("Embed Thumbnail"))

    # ------------------------------ logging ------------------------------ #

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _on_message(self, msg: _Message) -> None:
        if msg.kind == "log":
            self._log(msg.text)
            self.status_var.set(msg.text[:120])
        elif msg.kind == "done":
            self.progress.stop()
            self.progress.set(0)
            if msg.success:
                self.status_var.set("Done.")
            else:
                self.status_var.set("Finished with errors. See log.")
                messagebox.showerror(
                    "Job failed",
                    "The job finished with errors. Open the Log pane at the bottom "
                    "of the window for details.",
                )
            self._set_buttons_enabled(True)
            if self._last_output_dir and Path(self._last_output_dir).is_dir():
                self.open_folder_btn.configure(state="normal")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._action_buttons:
            btn.configure(state="normal" if enabled else "disabled")

    def _open_last_output_dir(self) -> None:
        if self._last_output_dir:
            _reveal_in_file_manager(self._last_output_dir)

    # ----------------------------- job dispatch -------------------------- #

    def _start_job(self, target, *, output_dir: str | None) -> None:
        if not self.runner.submit(target):
            self.status_var.set("A job is already running.")
            return
        self._last_output_dir = output_dir
        self.open_folder_btn.configure(state="disabled")
        self._set_buttons_enabled(False)
        self.progress.start()
        self.status_var.set("Running...")

    # --------------------------- tab construction ------------------------ #

    def _build_url_tab(
        self,
        parent,
        *,
        description: str,
        url_setting: str,
        dir_setting: str,
        button_label: str,
        download_fn: Callable,
        job_label: str,
    ) -> None:
        ctk.CTkLabel(parent, text=description, anchor="w").pack(
            fill="x", padx=10, pady=(8, 4),
        )

        ctk.CTkLabel(parent, text="YouTube URL(s) — one per line:", anchor="w").pack(
            fill="x", padx=10,
        )
        url_box = ctk.CTkTextbox(parent, height=80)
        url_box.pack(fill="x", padx=10, pady=4)
        url_box.insert("1.0", self.settings.get(url_setting))

        out_var = _path_row(
            parent, "Output dir:",
            self.settings.get(dir_setting),
            lambda v: self.settings.set(dir_setting, v),
        )

        def go() -> None:
            text = url_box.get("1.0", "end").strip()
            self.settings.set(url_setting, text)
            urls = parse_urls(text)
            if not urls:
                messagebox.showinfo("No URLs", "Enter at least one URL (one per line).")
                return
            out_dir = out_var.get().strip()
            if not out_dir:
                messagebox.showinfo("No output dir", "Pick an output directory.")
                return
            cookies = self._cookies_var.get().strip()

            def target(p):
                result = download_fn(urls, out_dir, cookies_path=cookies or None, progress=p)
                self._wrap_result(job_label, result, p)

            self._start_job(target, output_dir=out_dir)

        btn = ctk.CTkButton(parent, text=button_label, command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._action_buttons.append(btn)

    def _build_embed_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text=("Embed a new thumbnail into an existing MP3.\n"
                  "  • Folder mode: pick an audio folder + a thumbnail folder. "
                  "Files are matched by basename (Song.mp3 + Song.jpg).\n"
                  "  • Single file mode: pick one audio file and one thumbnail image."),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=10, pady=(8, 4))

        mode_var = ctk.StringVar(value=self.settings.get("embed_mode") or "folder")
        mode_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mode_frame.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(mode_frame, text="Mode:", width=120, anchor="w").pack(side="left")

        # Build rows once and swap their kind via state on mode change.
        # Simplest correct approach: tear down and rebuild the rows.
        rows_frame = ctk.CTkFrame(parent, fg_color="transparent")
        rows_frame.pack(fill="x")

        state: dict[str, ctk.StringVar] = {}

        def rebuild_rows() -> None:
            for child in rows_frame.winfo_children():
                child.destroy()
            mode = mode_var.get()
            if mode == "folder":
                state["video"] = _path_row(
                    rows_frame, "Audio folder:",
                    self.settings.get("embed_video_dir"),
                    lambda v: self.settings.set("embed_video_dir", v),
                    kind="folder",
                )
                state["thumb"] = _path_row(
                    rows_frame, "Thumb folder:",
                    self.settings.get("embed_thumb_dir"),
                    lambda v: self.settings.set("embed_thumb_dir", v),
                    kind="folder",
                )
            else:
                state["video"] = _path_row(
                    rows_frame, "Audio file:",
                    self.settings.get("embed_video_dir"),
                    lambda v: self.settings.set("embed_video_dir", v),
                    kind="file",
                    file_types=[
                        ("Audio", "*.mp3 *.m4a *.wav *.flac *.ogg"),
                        ("All files", "*.*"),
                    ],
                )
                state["thumb"] = _path_row(
                    rows_frame, "Thumbnail:",
                    self.settings.get("embed_thumb_dir"),
                    lambda v: self.settings.set("embed_thumb_dir", v),
                    kind="file",
                    file_types=[
                        ("Images", "*.jpg *.jpeg *.png"),
                        ("All files", "*.*"),
                    ],
                )
            state["out"] = _path_row(
                rows_frame, "Output dir:",
                self.settings.get("embed_out_dir"),
                lambda v: self.settings.set("embed_out_dir", v),
                kind="folder",
            )

        def on_mode_change() -> None:
            self.settings.set("embed_mode", mode_var.get())
            rebuild_rows()

        ctk.CTkRadioButton(mode_frame, text="Folder", variable=mode_var, value="folder",
                           command=on_mode_change).pack(side="left", padx=4)
        ctk.CTkRadioButton(mode_frame, text="Single file", variable=mode_var, value="single",
                           command=on_mode_change).pack(side="left", padx=4)

        rebuild_rows()

        ctk.CTkLabel(
            parent,
            text="*Output directory must be different from the audio/thumb source.",
            anchor="w",
            text_color=("gray40", "gray70"),
        ).pack(fill="x", padx=10, pady=(2, 0))

        def go() -> None:
            mode = mode_var.get()
            video = state["video"].get().strip()
            thumb = state["thumb"].get().strip()
            out_dir = state["out"].get().strip()
            if not out_dir:
                messagebox.showinfo("No output dir", "Pick an output directory.")
                return
            if not video or not thumb:
                messagebox.showinfo(
                    "Missing input",
                    "Fill in both the audio and thumbnail paths.",
                )
                return

            if mode == "single":
                def target(p):
                    result = embed_single(video, thumb, out_dir, progress=p)
                    self._wrap_embed_result(result, p)
            else:
                def target(p):
                    result = embed_folder(video, thumb, out_dir, progress=p)
                    self._wrap_embed_result(result, p)

            self._start_job(target, output_dir=out_dir)

        btn = ctk.CTkButton(parent, text="Embed", command=go)
        btn.pack(fill="x", padx=10, pady=(8, 10))
        self._action_buttons.append(btn)

    # ----------------------------- result wrappers ----------------------- #

    def _wrap_result(self, label: str, result: DownloadResult, progress) -> None:
        if result.success:
            progress(f"[{label}] SUCCESS")
        else:
            progress(f"[{label}] FAILED: {result.message or 'see log above'}")

    def _wrap_embed_result(self, result: EmbedResult, progress) -> None:
        progress(
            f"[Embed] processed={result.processed} failed={result.failed} "
            f"out={result.output_dir}"
        )

    # --------------------------- startup checks -------------------------- #

    def _ffmpeg_preflight(self) -> None:
        """Warn the user once if ffmpeg isn't found anywhere we know to look."""
        ff = find_ffmpeg()
        if ff is None:
            messagebox.showwarning(
                "ffmpeg not found",
                "Could not find an ffmpeg binary on PATH or in common install "
                "locations. Downloads and thumbnail embedding will fail.\n\n"
                "Install ffmpeg (macOS: `brew install ffmpeg`) or set the "
                "FFMPEG_BINARY environment variable to its full path.",
            )
            self.status_var.set("ffmpeg not found — features will not work.")
        else:
            self._log(f"[startup] Using ffmpeg at {ff}")
