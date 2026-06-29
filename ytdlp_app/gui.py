"""customtkinter front-end.

Layout:

    +-----------------------------------------------------------+
    |  Tabs: [Download | Music | Embed Thumbnail | Settings]            |
    |                                                           |
    |  ... tab content ...                                      |
    |                                                           |
    +-----------------------------------------------------------+
    |  Active downloads (always visible)                        |
    +-----------------------------------------------------------+
    |  Recent jobs (collapsible)                                |
    +-----------------------------------------------------------+
    |  Log + status bar                                         |
    +-----------------------------------------------------------+
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from . import __version__, thumbcache
from .jobs import CANCELLED, DONE, FAILED, Job, JobQueue, QUEUED, RUNNING
from .runtime import find_ffmpeg
from .search import SearchResult, is_url
from .settings import Settings, _config_dir

_THUMB_SIZE = (120, 68)  # 16:9 thumbnail


_FORMAT_LABELS = {
    "audio": "Audio (MP3)",
    "video": "Video (MP4)",
    "thumb": "Thumbnail (JPG)",
}
_FORMAT_DIR_KEY = {
    "audio": "audio_dir",
    "video": "video_dir",
    "thumb": "thumb_dir",
}
_FORMAT_DEFAULT_KEY = {
    "audio": "default_audio",
    "video": "default_video",
    "thumb": "default_thumb",
}


# ============================================================================
# Helpers
# ============================================================================

def _pick_folder(initial: str = "") -> str:
    return filedialog.askdirectory(initialdir=initial or str(Path.home())) or ""


def _pick_file(initial: str = "", types: list[tuple[str, str]] | None = None) -> str:
    return filedialog.askopenfilename(
        initialdir=initial or str(Path.home()),
        filetypes=types or [("All files", "*.*")],
    ) or ""


def _reveal_in_file_manager(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(p)], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", str(p)], check=False)
    else:
        subprocess.run(["xdg-open", str(p)], check=False)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ============================================================================
# Main window
# ============================================================================

class App(ctk.CTk):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

        ctk.set_appearance_mode(self.settings.get("theme") or "system")
        ctk.set_default_color_theme("blue")

        self.title(f"YTDLP {__version__}")
        self.geometry("1000x900")
        self.minsize(900, 720)

        # ----- inter-thread message queue ----- #
        self._msg_q: queue.Queue[Job] = queue.Queue()

        # ----- job queue ----- #
        self.jobs = JobQueue(
            max_parallel=int(self.settings.get("max_parallel_downloads") or 2),
            listener=self._enqueue_job_update,
        )

        # ----- model state ----- #
        self.results: list[SearchResult] = []
        self._active_rows: dict[int, _ActiveRow] = {}
        self._recent_rows: dict[int, _RecentRow] = {}
        self._result_rows: list[_ResultRow] = []

        # Infinite-scroll state: only populated when the most recent op that
        # filled `self.results` was a successful YouTube text search. Paste/
        # resolve results don't get auto-load-more (the playlist is finite).
        self._search_query: str | None = None
        self._pending_search_query: str | None = None
        self._search_loading_more: bool = False
        self._search_more_exhausted: bool = False
        self._search_page_size: int = 20
        self._search_videos_only: bool = True
        self._search_audio_only: bool = bool(self.settings.get("search_audio_only"))
        self._loading_more_label: ctk.CTkLabel | None = None

        # Music tab state (separate from Download tab results).
        self.music_results: list[SearchResult] = []
        self._music_result_rows: list[_ResultRow] = []
        self._music_search_query: str | None = None
        self._music_pending_search_query: str | None = None
        self._music_search_loading_more: bool = False
        self._music_search_more_exhausted: bool = False
        self._music_search_page_size: int = 20
        self._music_loading_more_label: ctk.CTkLabel | None = None

        # ----- build UI -----
        # Pack order matters: bottom widgets are packed first (innermost first
        # when stacking from the same side). We want, top -> bottom:
        #     [ Tabs (expand) ]
        #     [ Active panel  ]
        #     [ Recent panel  ]
        #     [ Log pane      ]
        self._build_log_pane()       # side="bottom" — pinned to bottom
        self._build_recent_panel()   # side="bottom" — above log
        self._build_active_panel()   # side="bottom" — above recent
        self._build_tabs()           # side="top", expand=True — fills the top
        self._setup_scroll_forwarding()
        self._poll_msg_q()
        self._poll_scroll_bottom()
        self._ffmpeg_preflight()

        # graceful shutdown
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ====================== UI construction =================================

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(side="top", fill="both", expand=True, padx=10, pady=10)

        self.download_tab = self.tabs.add("Download")
        self.music_tab = self.tabs.add("Music")
        self.embed_tab = self.tabs.add("Embed Thumbnail")
        self.settings_tab = self.tabs.add("Settings")

        self._build_download_tab(self.download_tab)
        self._build_music_tab(self.music_tab)
        self._build_embed_tab(self.embed_tab)
        self._build_settings_tab(self.settings_tab)

    # ------------------------- Download tab ---------------------------------

    def _build_download_tab(self, parent) -> None:
        # Use grid so the results row can be told to expand and the format/
        # source rows stay at their natural height. Pack would let those two
        # eat all of the tab's vertical space and squash the results to ~1px.
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=0)  # formats
        parent.grid_rowconfigure(1, weight=0)  # source picker
        parent.grid_rowconfigure(2, weight=1)  # results (the only expander)

        # ---- format checkboxes (single compact row) ----
        fmt_frame = ctk.CTkFrame(parent)
        fmt_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(fmt_frame, text="Formats:", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=(10, 8), pady=6,
        )

        self.format_vars: dict[str, ctk.BooleanVar] = {}
        self._format_dir_labels: dict[str, ctk.CTkLabel] = {}
        for fmt in ("audio", "video", "thumb"):
            var = ctk.BooleanVar(value=bool(self.settings.get(_FORMAT_DEFAULT_KEY[fmt])))
            self.format_vars[fmt] = var

            def _on_toggle(f=fmt):
                self.settings.set(_FORMAT_DEFAULT_KEY[f], self.format_vars[f].get())

            ctk.CTkCheckBox(fmt_frame, text=_FORMAT_LABELS[fmt],
                            variable=var, command=_on_toggle).pack(
                side="left", padx=8, pady=6,
            )

        ctk.CTkButton(
            fmt_frame, text="Output folders…", width=140,
            fg_color="transparent", border_width=1,
            command=lambda: self.tabs.set("Settings"),
        ).pack(side="right", padx=10, pady=6)

        # ---- source sub-tabs ----
        self.source_tabs = ctk.CTkTabview(parent, height=140)
        self.source_tabs.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
        self._build_search_subtab(self.source_tabs.add("Search YouTube"))
        self._build_paste_subtab(self.source_tabs.add("Paste URLs"))
        last = self.settings.get("source_tab") or "search"
        self.source_tabs.set(
            "Search YouTube" if last == "search" else "Paste URLs"
        )

        # ---- results list (expanding row) ----
        res_outer = ctk.CTkFrame(parent)
        res_outer.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 8))
        res_outer.grid_columnconfigure(0, weight=1)
        res_outer.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(res_outer, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        self.results_header_label = ctk.CTkLabel(
            header, text="Results (0) — search or paste a URL above",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        )
        self.results_header_label.pack(side="left", padx=4)
        ctk.CTkButton(header, text="Clear", width=70,
                      command=self._clear_results).pack(side="right", padx=2)
        ctk.CTkButton(header, text="📁", width=44,
                      command=lambda: self._download_all(override=True)).pack(
            side="right", padx=2,
        )
        ctk.CTkButton(header, text="Download all", width=130,
                      command=lambda: self._download_all(override=False)).pack(
            side="right", padx=2,
        )

        self.results_frame = ctk.CTkScrollableFrame(res_outer)
        self.results_frame.grid(row=1, column=0, sticky="nsew",
                                padx=6, pady=(0, 6))
        self._render_results()

    def _build_search_subtab(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(8, 2))

        self.search_var = ctk.StringVar(value=self.settings.get("search_query"))
        entry = ctk.CTkEntry(row, textvariable=self.search_var,
                             placeholder_text="Type a query, or paste a YouTube URL")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self._do_search())

        ctk.CTkButton(row, text="Search", width=100,
                      command=self._do_search).pack(side="left", padx=2)

        ctk.CTkLabel(row, text="Limit:").pack(side="left", padx=(8, 2))
        self.limit_var = ctk.StringVar(value=str(self.settings.get("search_limit") or 20))
        ctk.CTkOptionMenu(row, values=["10", "20", "50"],
                          variable=self.limit_var, width=70,
                          command=self._on_limit_change).pack(side="left", padx=2)

        # Filter row. Channels/playlists are *always* filtered out — they
        # aren't downloadable as a single track and only confuse the list.
        # Only "Prefer audio" is user-togglable.
        filt_row = ctk.CTkFrame(parent, fg_color="transparent")
        filt_row.pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(filt_row, text="Filters:",
                     text_color=("gray40", "gray70")).pack(side="left", padx=(2, 6))

        self.filter_audio_only_var = ctk.BooleanVar(
            value=bool(self.settings.get("search_audio_only"))
        )
        ctk.CTkCheckBox(
            filt_row,
            text="Prefer audio (skip music videos & lives)",
            variable=self.filter_audio_only_var,
            command=lambda: self.settings.set(
                "search_audio_only", self.filter_audio_only_var.get()
            ),
        ).pack(side="left", padx=6)

        ctk.CTkLabel(
            filt_row,
            text="(channels & playlists are always hidden)",
            text_color=("gray50", "gray60"),
        ).pack(side="left", padx=(12, 0))

        hint = ctk.CTkLabel(
            parent,
            text="Press Enter or click Search. Pasting a full URL resolves it directly "
                 "(no search query). Scroll to the bottom of the results for more.",
            text_color=("gray40", "gray70"),
            anchor="w",
        )
        hint.pack(fill="x", padx=12, pady=(0, 4))

    def _build_paste_subtab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Paste one URL per line. Playlists/channels expand into individual videos.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 2))

        self.paste_box = ctk.CTkTextbox(parent, height=55)
        self.paste_box.pack(fill="x", padx=10, pady=4)
        self.paste_box.insert("1.0", self.settings.get("paste_urls") or "")

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkButton(btn_row, text="Resolve & Pick", width=160,
                      command=self._do_resolve).pack(side="left", padx=2)
        ctk.CTkButton(btn_row, text="Download all immediately", width=210,
                      command=lambda: self._paste_download_all(override=False)).pack(
            side="left", padx=2,
        )
        ctk.CTkButton(btn_row, text="📁", width=44,
                      command=lambda: self._paste_download_all(override=True)).pack(
            side="left", padx=2,
        )

    # ------------------------- Music tab ------------------------------------

    def _build_music_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=0)
        parent.grid_rowconfigure(1, weight=0)
        parent.grid_rowconfigure(2, weight=1)

        opts_frame = ctk.CTkFrame(parent)
        opts_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(
            opts_frame, text="Music mode:", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).pack(side="left", padx=(10, 8), pady=6)

        self.music_lyrics_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_download_lyrics")),
        )
        ctk.CTkCheckBox(
            opts_frame,
            text="Download lyrics",
            variable=self.music_lyrics_var,
            command=lambda: self.settings.set(
                "music_download_lyrics", self.music_lyrics_var.get(),
            ),
        ).pack(side="left", padx=8, pady=6)

        ctk.CTkLabel(
            opts_frame,
            text="MP3 · title filename · metadata auto-applied",
            text_color=("gray40", "gray70"),
        ).pack(side="left", padx=(4, 8), pady=6)

        ctk.CTkButton(
            opts_frame, text="Output folder…", width=140,
            fg_color="transparent", border_width=1,
            command=lambda: self.tabs.set("Settings"),
        ).pack(side="right", padx=10, pady=6)

        self.music_source_tabs = ctk.CTkTabview(parent, height=140)
        self.music_source_tabs.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
        self._build_music_search_subtab(self.music_source_tabs.add("Search YouTube"))
        self._build_music_paste_subtab(self.music_source_tabs.add("Paste URLs"))
        last = self.settings.get("music_source_tab") or "search"
        self.music_source_tabs.set(
            "Search YouTube" if last == "search" else "Paste URLs",
        )

        res_outer = ctk.CTkFrame(parent)
        res_outer.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 8))
        res_outer.grid_columnconfigure(0, weight=1)
        res_outer.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(res_outer, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        self.music_results_header_label = ctk.CTkLabel(
            header, text="Results (0) — search or paste a URL above",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        )
        self.music_results_header_label.pack(side="left", padx=4)
        ctk.CTkButton(header, text="Clear", width=70,
                      command=self._music_clear_results).pack(side="right", padx=2)
        ctk.CTkButton(header, text="📁", width=44,
                      command=lambda: self._music_download_all(override=True)).pack(
            side="right", padx=2,
        )
        ctk.CTkButton(header, text="Download all", width=130,
                      command=lambda: self._music_download_all(override=False)).pack(
            side="right", padx=2,
        )

        self.music_results_frame = ctk.CTkScrollableFrame(res_outer)
        self.music_results_frame.grid(row=1, column=0, sticky="nsew",
                                      padx=6, pady=(0, 6))
        self._music_render_results()

    def _build_music_search_subtab(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(8, 2))

        self.music_search_var = ctk.StringVar(
            value=self.settings.get("music_search_query"),
        )
        entry = ctk.CTkEntry(
            row, textvariable=self.music_search_var,
            placeholder_text="Search for a song, or paste a YouTube URL",
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self._music_do_search())

        ctk.CTkButton(row, text="Search", width=100,
                      command=self._music_do_search).pack(side="left", padx=2)

        ctk.CTkLabel(row, text="Limit:").pack(side="left", padx=(8, 2))
        self.music_limit_var = ctk.StringVar(
            value=str(self.settings.get("music_search_limit") or 20),
        )
        ctk.CTkOptionMenu(
            row, values=["10", "20", "50"],
            variable=self.music_limit_var, width=70,
            command=self._on_music_limit_change,
        ).pack(side="left", padx=2)

        hint = ctk.CTkLabel(
            parent,
            text="Audio-only results (music videos and lives are filtered out). "
                 "Scroll to the bottom for more.",
            text_color=("gray40", "gray70"),
            anchor="w",
        )
        hint.pack(fill="x", padx=12, pady=(4, 4))

    def _build_music_paste_subtab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Paste one URL per line. Playlists expand into individual tracks.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 2))

        self.music_paste_box = ctk.CTkTextbox(parent, height=55)
        self.music_paste_box.pack(fill="x", padx=10, pady=4)
        self.music_paste_box.insert("1.0", self.settings.get("music_paste_urls") or "")

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkButton(btn_row, text="Resolve & Pick", width=160,
                      command=self._music_do_resolve).pack(side="left", padx=2)
        ctk.CTkButton(btn_row, text="Download all immediately", width=210,
                      command=lambda: self._music_paste_download_all(override=False)).pack(
            side="left", padx=2,
        )
        ctk.CTkButton(btn_row, text="📁", width=44,
                      command=lambda: self._music_paste_download_all(override=True)).pack(
            side="left", padx=2,
        )

    # ------------------------- Embed tab ------------------------------------

    def _build_embed_tab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text=("Embed a new thumbnail into existing audio files.\n"
                  "  • Folder mode: matches Song.mp3 + Song.jpg in two folders.\n"
                  "  • Single file mode: one audio file + one thumbnail."),
            anchor="w", justify="left",
        ).pack(fill="x", padx=10, pady=(8, 4))

        mode_var = ctk.StringVar(value=self.settings.get("embed_mode") or "folder")
        mode_row = ctk.CTkFrame(parent, fg_color="transparent")
        mode_row.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(mode_row, text="Mode:", width=120, anchor="w").pack(side="left")

        rows_frame = ctk.CTkFrame(parent, fg_color="transparent")
        rows_frame.pack(fill="x")

        embed_state: dict[str, ctk.StringVar] = {}

        def rebuild_rows() -> None:
            for child in rows_frame.winfo_children():
                child.destroy()
            mode = mode_var.get()
            if mode == "folder":
                embed_state["video"] = _path_row(
                    rows_frame, "Audio folder:",
                    self.settings.get("embed_video_dir"),
                    lambda v: self.settings.set("embed_video_dir", v),
                    kind="folder",
                )
                embed_state["thumb"] = _path_row(
                    rows_frame, "Thumb folder:",
                    self.settings.get("embed_thumb_dir"),
                    lambda v: self.settings.set("embed_thumb_dir", v),
                    kind="folder",
                )
            else:
                embed_state["video"] = _path_row(
                    rows_frame, "Audio file:",
                    self.settings.get("embed_video_dir"),
                    lambda v: self.settings.set("embed_video_dir", v),
                    kind="file",
                    file_types=[("Audio", "*.mp3 *.m4a *.wav *.flac *.ogg"),
                                ("All files", "*.*")],
                )
                embed_state["thumb"] = _path_row(
                    rows_frame, "Thumbnail:",
                    self.settings.get("embed_thumb_dir"),
                    lambda v: self.settings.set("embed_thumb_dir", v),
                    kind="file",
                    file_types=[("Images", "*.jpg *.jpeg *.png"),
                                ("All files", "*.*")],
                )
            embed_state["out"] = _path_row(
                rows_frame, "Output dir:",
                self.settings.get("embed_out_dir"),
                lambda v: self.settings.set("embed_out_dir", v),
                kind="folder",
            )

        def on_mode_change() -> None:
            self.settings.set("embed_mode", mode_var.get())
            rebuild_rows()

        ctk.CTkRadioButton(mode_row, text="Folder", variable=mode_var, value="folder",
                           command=on_mode_change).pack(side="left", padx=4)
        ctk.CTkRadioButton(mode_row, text="Single file", variable=mode_var,
                           value="single", command=on_mode_change).pack(side="left", padx=4)

        rebuild_rows()

        ctk.CTkLabel(
            parent,
            text="*Output directory must be different from the audio/thumb source.",
            anchor="w", text_color=("gray40", "gray70"),
        ).pack(fill="x", padx=10, pady=(2, 0))

        def go() -> None:
            mode = mode_var.get()
            video = embed_state["video"].get().strip()
            thumb = embed_state["thumb"].get().strip()
            out_dir = embed_state["out"].get().strip()
            if not out_dir:
                messagebox.showinfo("No output dir", "Pick an output directory.")
                return
            if not video or not thumb:
                messagebox.showinfo("Missing input",
                                    "Fill in both the audio and thumbnail paths.")
                return
            label = f"Embed: {Path(video).name}"
            kind = "embed_single" if mode == "single" else "embed_folder"
            params: dict[str, Any]
            if mode == "single":
                params = {"video": video, "thumb": thumb, "output_dir": out_dir}
            else:
                params = {"video_dir": video, "thumb_dir": thumb,
                          "output_dir": out_dir}
            self.jobs.enqueue(kind=kind, label=label, **params)

        ctk.CTkButton(parent, text="Embed", command=go).pack(
            fill="x", padx=10, pady=(8, 10),
        )

    # ------------------------- Settings tab ---------------------------------

    def _build_settings_tab(self, parent) -> None:
        scroll = ctk.CTkScrollableFrame(parent)
        scroll.pack(fill="both", expand=True, padx=8, pady=8)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(scroll, text=title,
                         font=ctk.CTkFont(weight="bold", size=14)).pack(
                fill="x", padx=10, pady=(10, 2), anchor="w",
            )
            frame = ctk.CTkFrame(scroll)
            frame.pack(fill="x", padx=10, pady=(0, 4))
            return frame

        # Output folders
        s_out = section("Default output folders")
        for fmt in ("audio", "video", "thumb"):
            def on_change(v, f=fmt):
                self.settings.set(_FORMAT_DIR_KEY[f], v)
                if f in self._format_dir_labels:
                    self._format_dir_labels[f].configure(text=v)
            _path_row(s_out, _FORMAT_LABELS[fmt] + ":",
                      self.settings.get(_FORMAT_DIR_KEY[fmt]),
                      on_change, kind="folder")

        s_music = section("Music mode")
        _path_row(s_music, "Music output folder:",
                  self.settings.get("music_dir"),
                  lambda v: self.settings.set("music_dir", v),
                  kind="folder")
        ctk.CTkLabel(
            s_music,
            text=("Downloads MP3 files named by track title. Artist, album, "
                  "cover art, and lyrics are written into file metadata."),
            text_color=("gray40", "gray70"), wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 8), anchor="w")

        # Cookies
        s_cookies = section("Cookies (optional)")
        _path_row(s_cookies, "Cookies file:",
                  self.settings.get("cookies_path"),
                  lambda v: self.settings.set("cookies_path", v),
                  kind="file",
                  file_types=[("Cookies (Netscape)", "*.txt"),
                              ("All files", "*.*")])
        ctk.CTkLabel(
            s_cookies,
            text=("Optional Netscape-format cookies file for age-restricted / "
                  "member-only videos. See cookies.txt.example for instructions."),
            text_color=("gray40", "gray70"), wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 8), anchor="w")

        # Downloads
        s_dl = section("Downloads")

        row = ctk.CTkFrame(s_dl, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row, text="Max parallel downloads:", width=220,
                     anchor="w").pack(side="left")
        parallel_var = ctk.StringVar(
            value=str(self.settings.get("max_parallel_downloads") or 2),
        )

        def on_parallel(value):
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 2
            self.settings.set("max_parallel_downloads", n)
            self.jobs.set_max_parallel(n)

        ctk.CTkOptionMenu(row, values=["1", "2", "3", "4", "6"],
                          variable=parallel_var, width=80,
                          command=on_parallel).pack(side="left")

        row = ctk.CTkFrame(s_dl, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row, text="Default search result limit:", width=220,
                     anchor="w").pack(side="left")
        limit_var2 = ctk.StringVar(value=str(self.settings.get("search_limit") or 20))

        def on_limit2(value):
            try:
                self.settings.set("search_limit", int(value))
                if hasattr(self, "limit_var"):
                    self.limit_var.set(value)
            except (TypeError, ValueError):
                pass

        ctk.CTkOptionMenu(row, values=["10", "20", "50"],
                          variable=limit_var2, width=80,
                          command=on_limit2).pack(side="left")

        row = ctk.CTkFrame(s_dl, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 8))
        ctk.CTkLabel(row, text="Default formats on launch:", width=220,
                     anchor="w").pack(side="left")
        for fmt in ("audio", "video", "thumb"):
            var = ctk.BooleanVar(value=bool(self.settings.get(_FORMAT_DEFAULT_KEY[fmt])))

            def on_default(f=fmt, v=var):
                self.settings.set(_FORMAT_DEFAULT_KEY[f], v.get())
                # also reflect in the Download-tab checkbox immediately
                self.format_vars[f].set(v.get())
            ctk.CTkCheckBox(row, text=_FORMAT_LABELS[fmt], variable=var,
                            command=on_default).pack(side="left", padx=6)

        # Appearance
        s_appear = section("Appearance")
        row = ctk.CTkFrame(s_appear, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 8))
        ctk.CTkLabel(row, text="Theme:", width=220, anchor="w").pack(side="left")
        theme_var = ctk.StringVar(value=self.settings.get("theme") or "system")

        def on_theme(_value=None):
            t = theme_var.get()
            self.settings.set("theme", t)
            ctk.set_appearance_mode(t)

        for value, label in (("system", "System"), ("light", "Light"),
                             ("dark", "Dark")):
            ctk.CTkRadioButton(row, text=label, variable=theme_var,
                               value=value, command=on_theme).pack(
                side="left", padx=6,
            )

        # About
        s_about = section("About")
        ff = find_ffmpeg()
        try:
            import yt_dlp  # noqa: WPS433
            ytv = yt_dlp.version.__version__
        except Exception:  # noqa: BLE001
            ytv = "unknown"
        about_lines = [
            f"ytdlp-app   {__version__}",
            f"yt-dlp      {ytv}",
            f"ffmpeg      {ff or '(not found — install via brew install ffmpeg)'}",
            f"Settings    {self.settings.path}",
            f"Data dir    {_config_dir()}",
        ]
        for line in about_lines:
            ctk.CTkLabel(s_about, text=line, anchor="w").pack(
                fill="x", padx=14, pady=2,
            )

        # Reset
        ctk.CTkButton(scroll, text="Reset all settings to defaults",
                      command=self._confirm_reset,
                      fg_color="transparent", border_width=1).pack(
            fill="x", padx=10, pady=(10, 10),
        )

    # ------------------------- bottom panels --------------------------------

    # ---- Heights used by the three collapsible bottom panels.
    _ACTIVE_EXPANDED_H = 150
    _RECENT_EXPANDED_H = 110
    _LOG_EXPANDED_H = 130
    _COLLAPSED_H = 36

    def _build_active_panel(self) -> None:
        # Fixed-height outer so an empty CTkScrollableFrame doesn't claim
        # 250+px of vertical space and squash the tabs above it.
        outer = ctk.CTkFrame(self, height=self._ACTIVE_EXPANDED_H)
        outer.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        outer.pack_propagate(False)
        self._active_outer = outer

        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", padx=6, pady=(4, 2))
        self.active_header = ctk.CTkLabel(
            header, text="Active downloads (0)", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        )
        self.active_header.pack(side="left", padx=4)
        self._active_toggle_btn = ctk.CTkButton(
            header, text="Hide ▾", width=80,
            command=self._toggle_active,
        )
        self._active_toggle_btn.pack(side="right", padx=2)
        ctk.CTkButton(header, text="Cancel all", width=110,
                      command=self.jobs.cancel_all).pack(side="right", padx=2)

        self.active_frame = ctk.CTkScrollableFrame(outer)
        self.active_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self._active_collapsed = False
        if self.settings.get("panel_active_collapsed"):
            self._toggle_active()

    def _build_recent_panel(self) -> None:
        outer = ctk.CTkFrame(self, height=self._RECENT_EXPANDED_H)
        outer.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        outer.pack_propagate(False)
        self._recent_outer = outer

        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", padx=6, pady=(4, 2))
        self._recent_collapsed = False

        self.recent_header = ctk.CTkLabel(
            header, text="Recent (0)", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        )
        self.recent_header.pack(side="left", padx=4)
        self._recent_toggle_btn = ctk.CTkButton(
            header, text="Hide ▾", width=80,
            command=self._toggle_recent,
        )
        self._recent_toggle_btn.pack(side="right", padx=2)
        ctk.CTkButton(header, text="Clear", width=70,
                      command=self._clear_recent).pack(side="right", padx=2)

        self.recent_frame = ctk.CTkScrollableFrame(outer)
        self.recent_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        if self.settings.get("panel_recent_collapsed"):
            self._toggle_recent()

    def _build_log_pane(self) -> None:
        outer = ctk.CTkFrame(self, height=self._LOG_EXPANDED_H)
        outer.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        outer.pack_propagate(False)
        self._log_outer = outer

        bar = ctk.CTkFrame(outer, fg_color="transparent")
        bar.pack(fill="x", padx=6, pady=(2, 0))
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(bar, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=4,
        )
        self._log_toggle_btn = ctk.CTkButton(
            bar, text="Hide log ▾", width=100,
            command=self._toggle_log,
        )
        self._log_toggle_btn.pack(side="right", padx=2)
        ctk.CTkButton(bar, text="Clear", width=70,
                      command=self._clear_log).pack(side="right", padx=2)

        self.log_box = ctk.CTkTextbox(outer, height=80, wrap="none")
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.log_box.configure(state="disabled")

        self._log_collapsed = False
        if self.settings.get("panel_log_collapsed"):
            self._toggle_log()

    # ====================== Actions ========================================

    def _checked_formats(self) -> list[str]:
        return [f for f in ("audio", "video", "thumb") if self.format_vars[f].get()]

    def _on_limit_change(self, value: str) -> None:
        try:
            self.settings.set("search_limit", int(value))
        except (TypeError, ValueError):
            pass

    def _on_music_limit_change(self, value: str) -> None:
        try:
            self.settings.set("music_search_limit", int(value))
        except (TypeError, ValueError):
            pass

    def _music_job_params(self) -> dict[str, Any]:
        return {
            "download_lyrics": bool(self.music_lyrics_var.get()),
            "enrich_metadata": True,
        }

    def _enqueue_music_download(
        self,
        url: str,
        label: str,
        *,
        out_dir: str,
        cookies: str | None,
    ) -> None:
        self.jobs.enqueue(
            kind="music",
            label=label,
            url=url,
            output_dir=out_dir,
            cookies_path=cookies,
            **self._music_job_params(),
        )

    def _do_search(self) -> None:
        query = self.search_var.get().strip()
        self.settings.set("search_query", query)
        self.settings.set("source_tab", "search")
        if not query:
            self._set_status("Enter a search query or URL.")
            return
        try:
            limit = int(self.limit_var.get())
        except (TypeError, ValueError):
            limit = 20
        cookies = self.settings.get("cookies_path") or None
        # Reset infinite-scroll state for the new query. Do NOT assign the
        # query to `_search_query` here — the old results may still be on
        # screen and a scroll-to-bottom would otherwise trigger a load-more
        # against the new query with stale result counts.
        self._search_query = None
        self._search_loading_more = False
        self._search_more_exhausted = False
        self._search_page_size = max(10, limit)
        self._pending_search_query = query if not is_url(query) else None
        # Snapshot the filter flags at search time so a paginated "load more"
        # uses the same filters even if the user toggles them later. Videos-
        # only is always on (channel/playlist entries aren't downloadable).
        self._search_videos_only = True
        self._search_audio_only = bool(self.filter_audio_only_var.get())
        label = f"Search: {query[:60]}"
        self.jobs.enqueue(
            kind="search", label=label,
            query=query, limit=limit,
            cookies_path=cookies,
            videos_only=self._search_videos_only,
            audio_only=self._search_audio_only,
        )

    def _music_do_search(self) -> None:
        query = self.music_search_var.get().strip()
        self.settings.set("music_search_query", query)
        self.settings.set("music_source_tab", "search")
        if not query:
            self._set_status("Enter a search query or URL.")
            return
        try:
            limit = int(self.music_limit_var.get())
        except (TypeError, ValueError):
            limit = 20
        cookies = self.settings.get("cookies_path") or None
        self._music_search_query = None
        self._music_search_loading_more = False
        self._music_search_more_exhausted = False
        self._music_search_page_size = max(10, limit)
        self._music_pending_search_query = query if not is_url(query) else None
        label = f"Music search: {query[:60]}"
        self.jobs.enqueue(
            kind="search", label=label,
            query=query, limit=limit,
            cookies_path=cookies,
            videos_only=True,
            audio_only=True,
            results_context="music",
        )

    def _do_resolve(self) -> None:
        text = self.paste_box.get("1.0", "end").strip()
        self.settings.set("paste_urls", text)
        self.settings.set("source_tab", "paste")
        urls = [u for u in text.splitlines() if u.strip() and is_url(u.strip())]
        if not urls:
            self._set_status("Paste one or more YouTube URLs first.")
            return
        cookies = self.settings.get("cookies_path") or None
        self.jobs.enqueue(
            kind="resolve", label=f"Resolve {len(urls)} URL(s)",
            urls=urls, cookies_path=cookies,
        )

    def _music_do_resolve(self) -> None:
        text = self.music_paste_box.get("1.0", "end").strip()
        self.settings.set("music_paste_urls", text)
        self.settings.set("music_source_tab", "paste")
        urls = [u for u in text.splitlines() if u.strip() and is_url(u.strip())]
        if not urls:
            self._set_status("Paste one or more YouTube URLs first.")
            return
        cookies = self.settings.get("cookies_path") or None
        self.jobs.enqueue(
            kind="resolve", label=f"Music resolve {len(urls)} URL(s)",
            urls=urls, cookies_path=cookies,
            results_context="music",
        )

    def _paste_download_all(self, *, override: bool) -> None:
        text = self.paste_box.get("1.0", "end").strip()
        self.settings.set("paste_urls", text)
        urls = [u.strip() for u in text.splitlines() if u.strip() and is_url(u.strip())]
        if not urls:
            self._set_status("Paste one or more YouTube URLs first.")
            return
        formats = self._checked_formats()
        if not formats:
            messagebox.showinfo("No formats", "Tick at least one format above.")
            return
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return  # user cancelled
        cookies = self.settings.get("cookies_path") or None
        for url in urls:
            for fmt in formats:
                out_dir = out_override or self.settings.get(_FORMAT_DIR_KEY[fmt])
                if not out_dir:
                    messagebox.showinfo(
                        "Missing output folder",
                        f"Configure an output folder for {_FORMAT_LABELS[fmt]} "
                        "in Settings.",
                    )
                    return
                label = f"{fmt.upper()}: {_truncate(url, 80)}"
                self.jobs.enqueue(
                    kind=fmt, label=label,
                    url=url, output_dir=out_dir,
                    cookies_path=cookies,
                )

    def _music_paste_download_all(self, *, override: bool) -> None:
        text = self.music_paste_box.get("1.0", "end").strip()
        self.settings.set("music_paste_urls", text)
        urls = [u.strip() for u in text.splitlines() if u.strip() and is_url(u.strip())]
        if not urls:
            self._set_status("Paste one or more YouTube URLs first.")
            return
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return
        out_dir = out_override or self.settings.get("music_dir")
        if not out_dir:
            messagebox.showinfo(
                "Missing output folder",
                "Configure a music output folder in Settings.",
            )
            return
        cookies = self.settings.get("cookies_path") or None
        for url in urls:
            label = f"Music: {_truncate(url, 80)}"
            self._enqueue_music_download(
                url, label, out_dir=out_dir, cookies=cookies,
            )

    def _download_one(self, result: SearchResult, *, override: bool) -> None:
        formats = self._checked_formats()
        if not formats:
            messagebox.showinfo("No formats", "Tick at least one format above.")
            return
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return
        cookies = self.settings.get("cookies_path") or None
        for fmt in formats:
            out_dir = out_override or self.settings.get(_FORMAT_DIR_KEY[fmt])
            if not out_dir:
                messagebox.showinfo(
                    "Missing output folder",
                    f"Configure an output folder for {_FORMAT_LABELS[fmt]} "
                    "in Settings.",
                )
                return
            label = f"{fmt.upper()}: {_truncate(result.display_title(60), 60)}"
            self.jobs.enqueue(
                kind=fmt, label=label,
                url=result.url, output_dir=out_dir,
                cookies_path=cookies,
            )

    def _music_download_one(self, result: SearchResult, *, override: bool) -> None:
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return
        out_dir = out_override or self.settings.get("music_dir")
        if not out_dir:
            messagebox.showinfo(
                "Missing output folder",
                "Configure a music output folder in Settings.",
            )
            return
        cookies = self.settings.get("cookies_path") or None
        label = f"Music: {_truncate(result.display_title(60), 60)}"
        self._enqueue_music_download(
            result.url, label, out_dir=out_dir, cookies=cookies,
        )

    def _download_all(self, *, override: bool) -> None:
        if not self.results:
            self._set_status("No results to download.")
            return
        formats = self._checked_formats()
        if not formats:
            messagebox.showinfo("No formats", "Tick at least one format above.")
            return
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return
        cookies = self.settings.get("cookies_path") or None
        for result in self.results:
            for fmt in formats:
                out_dir = out_override or self.settings.get(_FORMAT_DIR_KEY[fmt])
                if not out_dir:
                    messagebox.showinfo(
                        "Missing output folder",
                        f"Configure an output folder for {_FORMAT_LABELS[fmt]} "
                        "in Settings.",
                    )
                    return
                label = f"{fmt.upper()}: {_truncate(result.display_title(60), 60)}"
                self.jobs.enqueue(
                    kind=fmt, label=label,
                    url=result.url, output_dir=out_dir,
                    cookies_path=cookies,
                )

    def _music_download_all(self, *, override: bool) -> None:
        if not self.music_results:
            self._set_status("No results to download.")
            return
        out_override = None
        if override:
            out_override = _pick_folder()
            if not out_override:
                return
        out_dir = out_override or self.settings.get("music_dir")
        if not out_dir:
            messagebox.showinfo(
                "Missing output folder",
                "Configure a music output folder in Settings.",
            )
            return
        cookies = self.settings.get("cookies_path") or None
        for result in self.music_results:
            label = f"Music: {_truncate(result.display_title(60), 60)}"
            self._enqueue_music_download(
                result.url, label, out_dir=out_dir, cookies=cookies,
            )

    # ====================== Rendering ======================================

    def _render_results(self) -> None:
        self._clear_loading_indicator()
        for row in self._result_rows:
            row.destroy()
        self._result_rows.clear()

        for r in self.results:
            self._result_rows.append(_ResultRow(self.results_frame, r, self))

        if self.results:
            self.results_header_label.configure(text=f"Results ({len(self.results)})")
        else:
            self.results_header_label.configure(
                text="Results (0) — search or paste a URL above"
            )

    def _clear_results(self) -> None:
        self.results = []
        self._search_query = None
        self._search_loading_more = False
        self._search_more_exhausted = False
        self._clear_loading_indicator()
        self._render_results()

    def _music_render_results(self) -> None:
        self._music_clear_loading_indicator()
        for row in self._music_result_rows:
            row.destroy()
        self._music_result_rows.clear()

        for r in self.music_results:
            self._music_result_rows.append(
                _ResultRow(self.music_results_frame, r, self, mode="music"),
            )

        if self.music_results:
            self.music_results_header_label.configure(
                text=f"Results ({len(self.music_results)})",
            )
        else:
            self.music_results_header_label.configure(
                text="Results (0) — search or paste a URL above",
            )

    def _music_clear_results(self) -> None:
        self.music_results = []
        self._music_search_query = None
        self._music_search_loading_more = False
        self._music_search_more_exhausted = False
        self._music_clear_loading_indicator()
        self._music_render_results()

    # ====================== Infinite scroll =================================

    def _poll_scroll_bottom(self) -> None:
        """Detect when the user has scrolled near the bottom of results
        and kick off a `search_more` job to append the next page."""
        try:
            if self.tabs.get() == "Download":
                if (
                    self._search_query
                    and not self._search_loading_more
                    and not self._search_more_exhausted
                    and self.results
                ):
                    canvas = self.results_frame._parent_canvas  # noqa: SLF001
                    top, bottom = canvas.yview()
                    content_overflows = (bottom - top) < 0.999
                    if content_overflows and bottom >= 0.95:
                        self._load_more_results()
            elif self.tabs.get() == "Music":
                if (
                    self._music_search_query
                    and not self._music_search_loading_more
                    and not self._music_search_more_exhausted
                    and self.music_results
                ):
                    canvas = self.music_results_frame._parent_canvas  # noqa: SLF001
                    top, bottom = canvas.yview()
                    content_overflows = (bottom - top) < 0.999
                    if content_overflows and bottom >= 0.95:
                        self._music_load_more_results()
        except (AttributeError, ValueError):
            pass
        self.after(400, self._poll_scroll_bottom)

    def _load_more_results(self) -> None:
        if not self._search_query or self._search_loading_more:
            return
        self._search_loading_more = True
        already_loaded = len(self.results)
        new_limit = already_loaded + self._search_page_size
        cookies = self.settings.get("cookies_path") or None
        self._show_loading_indicator(
            f"Loading more results ({already_loaded} → {new_limit})..."
        )
        self.jobs.enqueue(
            kind="search_more",
            label=f"Load more: {self._search_query[:50]}",
            query=self._search_query,
            limit=new_limit,
            already_loaded=already_loaded,
            cookies_path=cookies,
            videos_only=self._search_videos_only,
            audio_only=self._search_audio_only,
        )

    def _music_load_more_results(self) -> None:
        if not self._music_search_query or self._music_search_loading_more:
            return
        self._music_search_loading_more = True
        already_loaded = len(self.music_results)
        new_limit = already_loaded + self._music_search_page_size
        cookies = self.settings.get("cookies_path") or None
        self._music_show_loading_indicator(
            f"Loading more results ({already_loaded} → {new_limit})...",
        )
        self.jobs.enqueue(
            kind="search_more",
            label=f"Music load more: {self._music_search_query[:50]}",
            query=self._music_search_query,
            limit=new_limit,
            already_loaded=already_loaded,
            cookies_path=cookies,
            videos_only=True,
            audio_only=True,
            results_context="music",
        )

    def _show_loading_indicator(self, text: str) -> None:
        self._clear_loading_indicator()
        self._loading_more_label = ctk.CTkLabel(
            self.results_frame, text=text,
            text_color=("gray40", "gray70"),
        )
        self._loading_more_label.pack(fill="x", padx=8, pady=8)
        self._bind_results_mousewheel(self._loading_more_label)

    def _clear_loading_indicator(self) -> None:
        if self._loading_more_label is not None:
            try:
                self._loading_more_label.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._loading_more_label = None

    def _music_show_loading_indicator(self, text: str) -> None:
        self._music_clear_loading_indicator()
        self._music_loading_more_label = ctk.CTkLabel(
            self.music_results_frame, text=text,
            text_color=("gray40", "gray70"),
        )
        self._music_loading_more_label.pack(fill="x", padx=8, pady=8)
        self._bind_results_mousewheel(self._music_loading_more_label)

    def _music_clear_loading_indicator(self) -> None:
        if self._music_loading_more_label is not None:
            try:
                self._music_loading_more_label.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._music_loading_more_label = None

    def _append_more_results(self, new_items: list[SearchResult]) -> None:
        for r in new_items:
            self.results.append(r)
            self._result_rows.append(_ResultRow(self.results_frame, r, self))
        self.results_header_label.configure(text=f"Results ({len(self.results)})")

    def _music_append_more_results(self, new_items: list[SearchResult]) -> None:
        for r in new_items:
            self.music_results.append(r)
            self._music_result_rows.append(
                _ResultRow(self.music_results_frame, r, self, mode="music"),
            )
        self.music_results_header_label.configure(
            text=f"Results ({len(self.music_results)})",
        )

    # ====================== Job listener ===================================

    def _enqueue_job_update(self, job: Job) -> None:
        """Called from worker threads. Push to the queue; UI thread will pick it up."""
        self._msg_q.put(job)

    def _poll_msg_q(self) -> None:
        try:
            while True:
                job = self._msg_q.get_nowait()
                self._handle_job_update(job)
        except queue.Empty:
            pass
        self.after(100, self._poll_msg_q)

    def _handle_job_update(self, job: Job) -> None:
        # Update or create the active row. `search_more` runs silently — the
        # user already sees the in-place "Loading more results..." indicator.
        if job.is_active:
            if job.kind != "search_more":
                row = self._active_rows.get(job.id)
                if row is None:
                    row = _ActiveRow(self.active_frame, job, self)
                    self._active_rows[job.id] = row
                row.update(job)
            # In-line search progress in the Results header so the user
            # has visible feedback without watching the bottom panel.
            # search_more updates the dedicated bottom indicator instead.
            ctx = job.params.get("results_context", "download")
            if job.kind == "search":
                hdr = (
                    self.music_results_header_label
                    if ctx == "music"
                    else self.results_header_label
                )
                hdr.configure(
                    text=f"Results — searching: {_truncate(job.progress_msg or '...', 60)}"
                )
            elif job.kind == "resolve":
                hdr = (
                    self.music_results_header_label
                    if ctx == "music"
                    else self.results_header_label
                )
                hdr.configure(
                    text=f"Results — resolving: {_truncate(job.progress_msg or '...', 60)}"
                )
        else:
            # Job is terminal — remove from active, add to recent (unless it was a search/resolve)
            row = self._active_rows.pop(job.id, None)
            if row is not None:
                row.frame.destroy()

            # Search / resolve completion: replace results list.
            if job.kind in ("search", "resolve"):
                ctx = job.params.get("results_context", "download")
                if job.state == DONE and isinstance(job.result, list):
                    if ctx == "music":
                        self.music_results = list(job.result)
                        self._music_render_results()
                    else:
                        self.results = list(job.result)
                        self._render_results()
                    self._set_status(
                        f"{job.kind.capitalize()} returned {len(job.result)} result(s)."
                    )
                    if job.kind == "search":
                        if ctx == "music":
                            self._music_search_query = self._music_pending_search_query
                            self._music_pending_search_query = None
                        else:
                            self._search_query = self._pending_search_query
                            self._pending_search_query = None
                    elif ctx == "music":
                        self._music_search_query = None
                    else:
                        self._search_query = None
                elif job.state == FAILED:
                    hdr = (
                        self.music_results_header_label
                        if ctx == "music"
                        else self.results_header_label
                    )
                    hdr.configure(text=f"Results — {job.kind} failed")
                    self._set_status(f"{job.kind.capitalize()} failed: {job.error}")
                elif job.state == CANCELLED:
                    hdr = (
                        self.music_results_header_label
                        if ctx == "music"
                        else self.results_header_label
                    )
                    hdr.configure(text=f"Results — {job.kind} cancelled")
                    self._set_status(f"{job.kind.capitalize()} cancelled.")

            # "Load more" completion: append only the new tail of results.
            elif job.kind == "search_more":
                ctx = job.params.get("results_context", "download")
                if ctx == "music":
                    self._music_search_loading_more = False
                    self._music_clear_loading_indicator()
                else:
                    self._search_loading_more = False
                    self._clear_loading_indicator()
                if job.state == DONE and isinstance(job.result, list):
                    already = int(job.params.get("already_loaded", 0))
                    new_items = job.result[already:]
                    if new_items:
                        if ctx == "music":
                            self._music_append_more_results(new_items)
                            self._set_status(
                                f"Loaded {len(new_items)} more "
                                f"(total {len(self.music_results)}).",
                            )
                        else:
                            self._append_more_results(new_items)
                            self._set_status(
                                f"Loaded {len(new_items)} more "
                                f"(total {len(self.results)}).",
                            )
                    elif ctx == "music":
                        self._music_search_more_exhausted = True
                        self._set_status("No more results.")
                    else:
                        self._search_more_exhausted = True
                        self._set_status("No more results.")
                elif job.state == FAILED:
                    self._set_status(f"Load more failed: {job.error}")
                elif job.state == CANCELLED:
                    pass
            else:
                # Download/embed: add to recent
                self._recent_rows[job.id] = _RecentRow(self.recent_frame, job, self)
                # Cap recent to last 10
                ids_sorted = sorted(self._recent_rows.keys(), reverse=True)
                for excess_id in ids_sorted[10:]:
                    rr = self._recent_rows.pop(excess_id, None)
                    if rr is not None:
                        rr.frame.destroy()
                if job.state == FAILED:
                    self._set_status(f"[fail] {job.label}: {job.error}")
                elif job.state == CANCELLED:
                    self._set_status(f"[cancel] {job.label}")
                else:
                    self._set_status(f"[done] {job.label}")

            self.jobs.clear_recent()  # let JobQueue drop its terminal copies

        # Update headers
        self.active_header.configure(text=f"Active downloads ({len(self._active_rows)})")
        self.recent_header.configure(text=f"Recent ({len(self._recent_rows)})")

        # Log line for any message change
        if job.progress_msg:
            self._log(f"[{job.state}] {job.label}: {job.progress_msg}")

    # ====================== Scroll-wheel forwarding =========================
    #
    # macOS + Tk 9.0 emits `<TouchpadScroll>` events for trackpad gestures
    # (which CTk 5.2 doesn't handle), while a real mouse wheel emits
    # `<MouseWheel>`. Windows/Linux only see `<MouseWheel>` / Button-4/-5.
    # We register a single global bind_all for each event type and figure
    # out which scrollable frame's canvas should receive the scroll by
    # walking up `event.widget.master`.
    #
    # The set of "scrollable canvases" is collected at build time from our
    # three CTkScrollableFrames: results, active downloads, recent jobs.

    def _setup_scroll_forwarding(self) -> None:
        import os
        debug = bool(os.environ.get("YTDLP_SCROLL_DEBUG"))

        # Collect the inner canvases that we want to scroll on wheel/touchpad.
        self._scroll_canvases: list = []
        for frame in (
            self.results_frame,
            self.music_results_frame,
            self.active_frame,
            self.recent_frame,
        ):
            try:
                self._scroll_canvases.append(frame._parent_canvas)  # noqa: SLF001
            except AttributeError:
                pass

        def _find_target_canvas(widget):
            seen = 0
            while widget is not None and seen < 100:
                if widget in self._scroll_canvases:
                    return widget
                widget = getattr(widget, "master", None)
                seen += 1
            return None

        def _scroll_units(canvas, units: int) -> str:
            try:
                canvas.yview_scroll(units, "units")
            except Exception:  # noqa: BLE001
                pass
            return "break"

        # On Tk 9 / macOS, a single trackpad tick fires both
        # <TouchpadScroll> AND a synthesized <MouseWheel>. The two events
        # can scroll in opposite directions and produce a "twitch then
        # reset" effect. We dedup by accepting only one scroll within a
        # short time window per event.time and event.serial.
        self._last_scroll_time_ms = 0

        def _accept_event(event) -> bool:
            # Paired TouchpadScroll + MouseWheel from the same gesture tick
            # arrive sub-millisecond apart on macOS Tk 9. A real next-tick
            # event comes ~16ms later (60 Hz reporting). A 5 ms window
            # catches the pair without throttling actual scrolling speed.
            t = int(getattr(event, "time", 0) or 0)
            if t and (t - self._last_scroll_time_ms) < 5:
                return False
            self._last_scroll_time_ms = t
            return True

        def _on_mousewheel(event):
            canvas = _find_target_canvas(event.widget)
            if debug:
                print(
                    f"[scroll] MouseWheel t={event.time} delta={event.delta} "
                    f"state={event.state} num={getattr(event,'num',0)} "
                    f"canvas={'yes' if canvas else 'no'}"
                )
            if canvas is None:
                return None
            if not _accept_event(event):
                return "break"
            num = getattr(event, "num", 0)
            if num == 4:
                return _scroll_units(canvas, -3)
            if num == 5:
                return _scroll_units(canvas, 3)
            delta = getattr(event, "delta", 0) or 0
            try:
                delta = int(delta)
            except (TypeError, ValueError):
                return "break"
            if delta == 0:
                return "break"
            if sys.platform == "darwin":
                step = max(-6, min(6, -delta))
                return _scroll_units(canvas, step)
            return _scroll_units(canvas, -int(delta / 120) * 3)

        def _on_touchpad_scroll(event):
            # Tk 9 macOS trackpad pan gesture. `event.delta` carries the
            # vertical pixel delta (dy). Positive dy = fingers moved down
            # gesture; for natural scrolling we want the canvas to follow
            # the gesture (page moves down with fingers).
            canvas = _find_target_canvas(event.widget)
            if debug:
                print(
                    f"[scroll] TouchpadScroll t={event.time} delta={event.delta} "
                    f"state={event.state} canvas={'yes' if canvas else 'no'}"
                )
            if canvas is None:
                return None
            if not _accept_event(event):
                return "break"
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return "break"
            # Convert pixel delta to canvas "units" (8 px on macOS).
            magnitude = max(1, min(6, abs(delta) // 8 or 1))
            units = magnitude if delta < 0 else -magnitude
            return _scroll_units(canvas, units)

        # Replace CTk's MouseWheel bind_all rather than augmenting it. CTk
        # walks ALL CTkScrollableFrames in the process per event and can
        # fight our handler when a gesture fires multiple synthetic events.
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:  # noqa: BLE001
            pass
        self.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind_all("<Button-4>", _on_mousewheel, add="+")
        self.bind_all("<Button-5>", _on_mousewheel, add="+")
        try:
            self.bind_all("<TouchpadScroll>", _on_touchpad_scroll, add="+")
        except Exception:  # noqa: BLE001
            pass

    def _bind_results_mousewheel(self, widget) -> None:
        # Kept as a public hook for places that previously called it
        # (loading indicator, result rows). With the global bind_all in
        # `_setup_scroll_forwarding`, no per-widget binding is needed.
        return

    # ====================== Misc UI =========================================

    def _set_status(self, text: str) -> None:
        self.status_var.set(text[:160])

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _clear_recent(self) -> None:
        for row in self._recent_rows.values():
            row.frame.destroy()
        self._recent_rows.clear()
        self.recent_header.configure(text="Recent (0)")

    def _toggle_recent(self) -> None:
        self._recent_collapsed = not self._recent_collapsed
        if self._recent_collapsed:
            self.recent_frame.pack_forget()
            self._recent_outer.configure(height=self._COLLAPSED_H)
            self._recent_toggle_btn.configure(text="Show ▸")
        else:
            self._recent_outer.configure(height=self._RECENT_EXPANDED_H)
            self.recent_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
            self._recent_toggle_btn.configure(text="Hide ▾")
        self.settings.set("panel_recent_collapsed", self._recent_collapsed)

    def _toggle_active(self) -> None:
        self._active_collapsed = not self._active_collapsed
        if self._active_collapsed:
            self.active_frame.pack_forget()
            self._active_outer.configure(height=self._COLLAPSED_H)
            self._active_toggle_btn.configure(text="Show ▸")
        else:
            self._active_outer.configure(height=self._ACTIVE_EXPANDED_H)
            self.active_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
            self._active_toggle_btn.configure(text="Hide ▾")
        self.settings.set("panel_active_collapsed", self._active_collapsed)

    def _toggle_log(self) -> None:
        self._log_collapsed = not self._log_collapsed
        if self._log_collapsed:
            self.log_box.pack_forget()
            self._log_outer.configure(height=self._COLLAPSED_H)
            self._log_toggle_btn.configure(text="Show log ▸")
        else:
            self._log_outer.configure(height=self._LOG_EXPANDED_H)
            self.log_box.pack(fill="both", expand=True, padx=6, pady=(2, 6))
            self._log_toggle_btn.configure(text="Hide log ▾")
        self.settings.set("panel_log_collapsed", self._log_collapsed)

    def _confirm_reset(self) -> None:
        if messagebox.askyesno(
            "Reset settings",
            "Reset all settings to their defaults?\n\n"
            "Your downloads are unaffected.",
        ):
            self.settings.reset_to_defaults()
            messagebox.showinfo("Reset", "Settings have been reset. "
                                "Relaunch the app to see all changes.")

    def _ffmpeg_preflight(self) -> None:
        if find_ffmpeg() is None:
            messagebox.showwarning(
                "ffmpeg not found",
                "Could not find an ffmpeg binary on PATH or in common install "
                "locations. Downloads and thumbnail embedding will fail.\n\n"
                "Install ffmpeg (macOS: `brew install ffmpeg`) or set the "
                "FFMPEG_BINARY environment variable to its full path.",
            )
            self._set_status("ffmpeg not found — features will not work.")

    def _on_close(self) -> None:
        try:
            self.jobs.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            thumbcache.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


# ============================================================================
# Sub-widgets: ResultRow, ActiveRow, RecentRow
# ============================================================================

class _ResultRow:
    """One row in the results list: thumbnail | title+meta | Download / 📁."""

    def __init__(
        self,
        parent,
        result: SearchResult,
        app: "App",
        *,
        mode: str = "download",
    ) -> None:
        self.result = result
        self.app = app
        self._alive = True
        self.frame = ctk.CTkFrame(parent)
        self.frame.pack(fill="x", padx=4, pady=3)

        if mode == "music":
            download_fn = lambda: app._music_download_one(result, override=False)
            folder_fn = lambda: app._music_download_one(result, override=True)
            btn_text = "Download"
        else:
            download_fn = lambda: app._download_one(result, override=False)
            folder_fn = lambda: app._download_one(result, override=True)
            btn_text = "Download"

        # Thumbnail (left).
        self._ctk_image = ctk.CTkImage(
            light_image=thumbcache.placeholder(_THUMB_SIZE),
            dark_image=thumbcache.placeholder(_THUMB_SIZE),
            size=_THUMB_SIZE,
        )
        self.thumb_label = ctk.CTkLabel(
            self.frame, text="", image=self._ctk_image,
            width=_THUMB_SIZE[0], height=_THUMB_SIZE[1],
        )
        self.thumb_label.pack(side="left", padx=(6, 8), pady=6)

        # Title + metadata (center).
        text_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True, padx=2, pady=6)
        ctk.CTkLabel(
            text_col, text=result.display_title(),
            anchor="w", font=ctk.CTkFont(weight="bold"),
            wraplength=500, justify="left",
        ).pack(fill="x")
        meta = result.metadata_line()
        if meta:
            ctk.CTkLabel(
                text_col, text=meta, anchor="w",
                text_color=("gray40", "gray70"),
            ).pack(fill="x")

        # Action buttons (right).
        btn_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        btn_col.pack(side="right", padx=6, pady=4)
        ctk.CTkButton(btn_col, text="📁", width=44,
                      command=folder_fn).pack(side="right", padx=2)
        ctk.CTkButton(btn_col, text=btn_text, width=110,
                      command=download_fn).pack(side="right", padx=2)

        # Forward mouse-wheel events from every child widget up to the
        # scrollable frame's canvas — otherwise the wheel does nothing once
        # the pointer is over a label/button/thumbnail.
        app._bind_results_mousewheel(self.frame)

        # Kick off the thumbnail fetch. The cache callback may fire on a
        # worker thread, so we hop back to the Tk main loop via `after`.
        self._kick_off_thumb_fetch()

    def _kick_off_thumb_fetch(self) -> None:
        url = self.result.thumbnail_url

        def _on_loaded(img) -> None:
            if not self._alive:
                return
            try:
                self.app.after(0, lambda: self._apply_thumb(img))
            except Exception:  # noqa: BLE001
                pass

        thumbcache.load(url, _on_loaded)

    def _apply_thumb(self, img) -> None:
        if not self._alive or img is None:
            return
        try:
            resized = img.resize(_THUMB_SIZE)
            self._ctk_image.configure(
                light_image=resized, dark_image=resized, size=_THUMB_SIZE,
            )
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        self._alive = False
        try:
            self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass


class _ActiveRow:
    def __init__(self, parent, job: Job, app: "App") -> None:
        self.frame = ctk.CTkFrame(parent)
        self.frame.pack(fill="x", padx=4, pady=2)

        top = ctk.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(4, 0))
        self.label = ctk.CTkLabel(top, text=job.label, anchor="w",
                                  font=ctk.CTkFont(weight="bold"))
        self.label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(top, text="×", width=32,
                      command=lambda: app.jobs.cancel(job.id)).pack(side="right")

        bottom = ctk.CTkFrame(self.frame, fg_color="transparent")
        bottom.pack(fill="x", padx=8, pady=(0, 6))
        self.bar = ctk.CTkProgressBar(bottom)
        self.bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.bar.set(0)
        self.status = ctk.CTkLabel(bottom, text=job.progress_msg or job.state,
                                   anchor="w",
                                   text_color=("gray40", "gray70"),
                                   width=380)
        self.status.pack(side="left")

    def update(self, job: Job) -> None:
        self.label.configure(text=job.label)
        self.bar.set(job.progress_pct / 100.0 if job.progress_pct else 0.0)
        msg = job.progress_msg or job.state
        self.status.configure(text=_truncate(msg, 120))


class _RecentRow:
    _GLYPH = {DONE: "✓", FAILED: "✗", CANCELLED: "—"}

    def __init__(self, parent, job: Job, app: "App") -> None:
        self.frame = ctk.CTkFrame(parent)
        self.frame.pack(fill="x", padx=4, pady=2)
        glyph = self._GLYPH.get(job.state, "·")
        text = f"{glyph}  {job.label}"
        if job.state == FAILED and job.error:
            text += f"  —  {job.error}"
        elif job.state == CANCELLED:
            text += "  —  cancelled"
        ctk.CTkLabel(self.frame, text=text, anchor="w",
                     justify="left", wraplength=900).pack(
            fill="x", padx=8, pady=4,
        )


# ============================================================================
# Generic path row used by Settings and Embed tabs
# ============================================================================

def _path_row(parent, label: str, value: str, on_change: Callable[[str], None],
              *, kind: str = "folder",
              file_types: list[tuple[str, str]] | None = None):
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", padx=10, pady=4)
    ctk.CTkLabel(frame, text=label, width=140, anchor="w").pack(side="left")
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
