"""Desktop media workspace: retained sidebar pages, unified search and a compact queue.

Network and media processing remain on the existing background job queues.
"""

from __future__ import annotations

import copy
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from . import __version__, thumbcache
from .ui import (
    ACCENT, ACCENT_FILL, HOVER, MUTED, PANEL, ROW, SIDEBAR, SURFACE, TEXT, VIOLET,
    SearchField, SidebarPages, SmoothProgress, SourcePanel, Tooltip, ArtworkLabel,
    artwork_image, install_theme,
)
from .imports import parse_import_links
from .jobs import CANCELLED, DONE, FAILED, Job, JobQueue, QUEUED, RUNNING
from .metadata.parse import parse_youtube_track
from .music_duplicates import check_music_duplicate, duplicate_location_label
from .runtime import apply_window_icon, find_ffmpeg
from .search import (
    SearchResult,
    _format_duration,
    _video_id_from_url,
    is_url,
    normalize_collection_title,
)
from .settings import Settings, _config_dir
from .sources import PLATFORM_CONFIGS, MusicTrack, platform_config
from .sources.base import MATCH_FAILED, MATCH_PENDING

_THUMB_SIZE = (120, 68)  # 16:9 thumbnail

_LOG_HEIGHTS = {"normal": 130, "large": 280, "xlarge": 450}
_LOG_HEIGHT_CYCLE = ("normal", "large", "xlarge")
_LOG_HEIGHT_LABELS = {
    "normal": "Size: Normal",
    "large": "Size: Large",
    "xlarge": "Size: X-Large",
}

_MAIN_TAB_NAMES = {
    "playlists": "Playlists",
    "identify": "Identify",
    "music": "Music",
    "download": "Video",
    "settings": "Settings",
}
_MAIN_TAB_KEYS = {v: k for k, v in _MAIN_TAB_NAMES.items()}
_RESULTS_PANEL_COLOR = SURFACE


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
_RECENT_KIND_LABEL = {
    "music": "Music",
    "audio": "MP3",
    "video": "Video",
    "thumb": "Thumb",
    "embed_single": "Embed",
    "embed_folder": "Embed",
}


# ============================================================================
# Helpers
# ============================================================================

def _detect_scroll_sign(settings) -> int:
    """Return +1 if scrolling should follow Tk's default convention, or -1
    if it should be inverted.

    The user can force a specific direction via the `scroll_direction`
    setting ("auto" | "natural" | "inverted"). In "auto" mode we read
    macOS's "Natural scrolling" preference. On other platforms we trust
    Tk's signs (+1).
    """
    pref = (settings.get("scroll_direction") or "auto").lower()
    if pref == "natural":
        return 1
    if pref == "inverted":
        return -1
    if sys.platform != "darwin":
        return 1
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "com.apple.swipescrolldirection"],
            capture_output=True, text=True, timeout=2,
        )
        value = out.stdout.strip()
        # The key is only stored when the user has explicitly toggled
        # "Natural scrolling" away from macOS's default (which is ON).
        #  "1" / missing => Natural scrolling ON (default sign)
        #  "0"           => Inverted from device => flip our sign
        if value == "0":
            return -1
        return 1
    except Exception:  # noqa: BLE001
        return 1


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


def _recent_dedupe_key(job: Job) -> str:
    """One recent row per video/song, even when audio+video+thumb all download."""
    url = str(job.params.get("url") or job.params.get("source_url") or "")
    vid = _video_id_from_url(url)
    if vid:
        return f"vid:{vid}"
    artist = (
        job.params.get("expected_artist")
        or job.params.get("source_uploader")
        or ""
    ).casefold().strip()
    title = (
        job.params.get("expected_title")
        or job.params.get("source_title")
        or ""
    ).casefold().strip()
    if artist or title:
        return f"music:{artist}\0{title}"
    return f"{job.kind}:{job.label.casefold()}"


def _recent_primary_title(job: Job) -> str:
    artist = (
        job.params.get("expected_artist")
        or job.params.get("source_uploader")
        or ""
    ).strip()
    title = (
        job.params.get("expected_title")
        or job.params.get("source_title")
        or ""
    ).strip()
    if artist and title:
        return f"{artist} — {title}"
    if title:
        return title
    label = job.label or ""
    for prefix in ("Music:", "AUDIO:", "VIDEO:", "THUMB:"):
        if label.upper().startswith(prefix.upper()):
            return label[len(prefix):].strip()
    return label or "(untitled)"


def _recent_metadata_line(job: Job, kinds: list[str]) -> str:
    bits: list[str] = []
    album = str(job.params.get("source_album") or "").strip()
    if album:
        bits.append(album)
    duration = job.params.get("source_duration_s") or job.params.get("expected_duration_s")
    if isinstance(duration, (int, float)) and int(duration) > 0:
        bits.append(_format_duration(int(duration)))
    fmt_labels = []
    for kind in kinds:
        label = _RECENT_KIND_LABEL.get(kind, kind.capitalize())
        if label not in fmt_labels:
            fmt_labels.append(label)
    if fmt_labels:
        bits.append(", ".join(fmt_labels))
    return " · ".join(bits)


# ============================================================================
# Main window
# ============================================================================

class App(ctk.CTk):
    def __init__(self, settings: Settings) -> None:
        install_theme()
        super().__init__()
        self.settings = settings

        ctk.set_appearance_mode(self.settings.get("theme") or "system")

        self.title(f"easy-dlp {__version__}")
        w = int(self.settings.get("window_width") or 1280)
        h = int(self.settings.get("window_height") or 1000)
        self.geometry(f"{w}x{h}")
        self.minsize(1080, 720)
        # After Tk is up — AppKit before CTk() crashes Tk 9 on macOS.
        apply_window_icon(self)

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
        self._recent_rows: dict[str, "_RecentRow"] = {}
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
        self.music_tracks: list[MusicTrack] = []
        self._music_showing_tracks = False
        self._music_auto_download = False
        self._music_pending_out_dir: str | None = None
        self._music_result_rows: list[_ResultRow] = []
        self._music_track_rows: list["_MusicTrackRow"] = []
        self._music_search_query: str | None = None
        self._music_pending_search_query: str | None = None
        self._music_search_loading_more: bool = False
        self._music_search_more_exhausted: bool = False
        self._music_search_page_size: int = 20
        self._music_track_count: int = 0
        self._music_album_count: int = 0
        self._music_albums_exhausted: bool = False
        self._music_search_audio_only: bool = bool(
            self.settings.get("music_search_audio_only"),
        )
        self._music_use_youtube_music: bool = bool(
            self.settings.get("music_use_youtube_music"),
        )
        self._music_search_include_albums: bool = bool(
            self.settings.get("music_include_albums", True),
        )
        self._music_stream_job_id: int | None = None
        self._music_match_job_id: int | None = None
        self._music_loading_more_label: ctk.CTkLabel | None = None
        self._music_scroll_footer: ctk.CTkButton | None = None
        self._search_scroll_footer: ctk.CTkButton | None = None
        self._results_scroll_sync_pending: dict[int, str | None] = {}
        self._music_alternate_open_index: int | None = None
        self._music_alternate_mode: str = "track"  # "track" | "search"
        self._music_alternate_panels: dict[int, "_MusicAlternatePanel"] = {}
        self._music_rematch_panel: "_MusicAlternatePanel | None" = None
        self._music_input_collapsed: bool = False
        self._music_pending_stream_albums: list[SearchResult] = []
        # Chunked result rendering — building all CTk rows in one go freezes
        # the UI for seconds on macOS.
        self._results_render_token = 0
        self._music_render_token = 0
        self._syncing_scroll_height = False
        self._RESULT_RENDER_CHUNK = 3

        # Prevent duplicate side-effects when terminal jobs are notified more
        # than once (e.g. nested Tk dialogs while the message queue drains).
        self._terminal_side_effects_handled: set[int] = set()
        self._pending_collection_urls: set[str] = set()
        self._batch_duplicate_dialog_open = False

        # Log panel state
        self._last_logged: dict[int, str] = {}
        self._last_log_time: dict[int, float] = {}
        self._log_popout: "_LogPopout | None" = None
        self._log_height = str(self.settings.get("panel_log_height") or "normal")
        if self._log_height not in _LOG_HEIGHTS:
            self._log_height = "normal"

        # ----- build UI -----
        # Bottom activity bar first, then the retained sidebar workspace.
        self._build_activity_dock()  # side="bottom" — single slim strip
        self._build_tabs()           # side="top", expand=True — fills the top
        self._setup_scroll_forwarding()
        self._poll_msg_q()
        self._poll_scroll_bottom()
        self._ffmpeg_preflight()
        modifier = "Command" if sys.platform == "darwin" else "Control"
        self.bind(f"<{modifier}-f>", self._focus_search)
        self.bind(f"<{modifier}-comma>", lambda _e: self.tabs.set("Settings"))
        self.bind("<Escape>", self._dismiss_detail)

        # graceful shutdown
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ====================== UI construction =================================

    def _build_tabs(self) -> None:
        self.tabs = SidebarPages(self, command=self._on_main_tab_changed,
                                 activity_command=self._open_activity)
        self.tabs.pack(side="top", fill="both", expand=True)

        self.music_tab = self.tabs.add("Music")
        self.download_tab = self.tabs.add("Video")
        self.playlists_tab = self.tabs.add("Playlists")
        from .playlist_ui import PlaylistsPage
        self.playlists_page = PlaylistsPage(self.playlists_tab, self)
        self.identify_tab = self.tabs.add("Identify")
        from .discovery_ui import IdentifyPage
        self.identify_page = IdentifyPage(self.identify_tab, self)
        self.settings_tab = self.tabs.add("Settings")

        self._build_music_tab(self.music_tab)
        self._build_download_tab(self.download_tab)
        self._build_settings_tab(self.settings_tab)

        tab_key = str(self.settings.get("main_tab") or "music")
        if tab_key not in _MAIN_TAB_NAMES:
            tab_key = "settings" if tab_key == "embed" else "music"
        self.tabs.set(_MAIN_TAB_NAMES.get(tab_key, "Music"))

    # ------------------------- Download tab ---------------------------------

    def _build_download_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=0)  # options bar
        parent.grid_rowconfigure(1, weight=0)  # source picker
        parent.grid_rowconfigure(2, weight=1)  # results

        # ---- collapsible format options ----
        self._download_options_collapsed = bool(
            self.settings.get("download_options_collapsed", True),
        )
        opts_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        opts_wrap.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        opts_bar = ctk.CTkFrame(opts_wrap, fg_color="transparent")
        opts_bar.pack(fill="x")
        ctk.CTkLabel(
            opts_bar, text="Video", anchor="w",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).pack(side="left", padx=4)
        self._download_options_toggle_btn = ctk.CTkButton(
            opts_bar, text="Options…", width=120,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._toggle_download_options,
        )
        self._download_options_toggle_btn.pack(side="right", padx=2)

        self._download_options_body = ctk.CTkFrame(opts_wrap)
        fmt_frame = ctk.CTkFrame(self._download_options_body, fg_color="transparent")
        fmt_frame.pack(fill="x", padx=6, pady=(4, 6))
        ctk.CTkLabel(fmt_frame, text="Formats:", anchor="w").pack(
            side="left", padx=(4, 8), pady=4,
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
                side="left", padx=8, pady=4,
            )

        ctk.CTkButton(
            fmt_frame, text="Output folders…", width=140,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=lambda: self.tabs.set("Settings"),
        ).pack(side="right", padx=6, pady=4)

        if self._download_options_collapsed:
            self._set_download_options_collapsed(True)
        else:
            self._download_options_body.pack(fill="x", pady=(2, 0))

        # ---- source input (always visible) ----
        input_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        input_wrap.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))

        self.source_tabs = SourcePanel(input_wrap, bulk_label="Import links…")
        self.source_tabs.pack(fill="x")
        self._build_search_subtab(self.source_tabs.add("Search YouTube"))
        self._build_paste_subtab(self.source_tabs.add("Paste URLs"))
        last = self.settings.get("source_tab") or "search"
        self.source_tabs.set(
            "Search YouTube" if last == "search" else "Paste URLs"
        )

        # ---- results list (expanding row) ----
        res_outer = ctk.CTkFrame(parent, fg_color=_RESULTS_PANEL_COLOR, border_width=0)
        res_outer.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 8))
        res_outer.grid_columnconfigure(0, weight=1)
        res_outer.grid_rowconfigure(1, weight=1)
        res_outer.grid_rowconfigure(2, weight=0)

        header = ctk.CTkFrame(res_outer, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.results_header_label = ctk.CTkLabel(
            header, text="Results",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        )
        self.results_header_label.pack(side="left", padx=4)
        ctk.CTkButton(header, text="Clear", width=70,
                      fg_color="transparent", text_color=MUTED, hover_color=HOVER,
                      command=self._clear_results).pack(side="right", padx=2)
        self._download_more_btn = ctk.CTkButton(
            header, text="⋯", width=36,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._show_download_more_menu,
        )
        self._download_more_btn.pack(side="right", padx=2)
        self._video_download_all_btn = ctk.CTkButton(
            header, text="Download all", width=150, height=32,
            command=lambda: self._download_all(override=False),
        )
        self._video_download_all_btn.pack(side="right", padx=2)

        self._download_results_body = ctk.CTkFrame(res_outer, fg_color="transparent")
        self._download_results_body.grid(row=1, column=0, sticky="nsew", padx=6, pady=0)
        self._download_results_body.grid_columnconfigure(0, weight=1)
        self._download_results_body.grid_rowconfigure(0, weight=1)

        self.results_frame = ctk.CTkScrollableFrame(self._download_results_body)
        self.results_frame.pack(fill="both", expand=True)
        self._bind_results_scroll_resize(self._download_results_body, self.results_frame)
        self.results_footer = ctk.CTkFrame(
            res_outer, fg_color="transparent", height=0,
        )
        self.results_footer.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))
        self._bind_results_pagination_watch(self.results_frame)
        self._video_empty = self._make_empty_state(self._download_results_body, "Find your next video", "Search YouTube or paste a link above.\nImport multiple links to build a download queue.")
        self._render_results()

    def _build_search_subtab(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(4, 2))

        self.search_var = ctk.StringVar(value=self.settings.get("search_query"))
        entry = SearchField(row, textvariable=self.search_var,
                             height=36, placeholder_text="Search videos or paste a YouTube link")
        self.search_entry = entry
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self._submit_video_input())

        self._video_submit_btn = ctk.CTkButton(row, text="Search", width=110,
                                              height=36, command=self._submit_video_input)
        self._video_submit_btn.pack(side="left", padx=2)
        self.search_var.trace_add("write", lambda *_args: self._refresh_actions())

        ctk.CTkLabel(row, text="Show:").pack(side="left", padx=(8, 2))
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
                     text_color=MUTED).pack(side="left", padx=(2, 6))

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

    def _build_paste_subtab(self, parent) -> None:
        ctk.CTkLabel(
            parent,
            text="Paste video or playlist links, one per line.",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 2))

        self.paste_box = ctk.CTkTextbox(parent, height=55)
        self.paste_box.pack(fill="x", padx=10, pady=4)
        self.paste_box.insert("1.0", self.settings.get("paste_urls") or "")

        self._video_import_feedback = ctk.CTkLabel(
            parent, text="Import the links above into the results list, then choose what to download.",
            anchor="w", text_color=MUTED, wraplength=680, justify="left",
        )
        self._video_import_feedback.pack(fill="x", padx=12, pady=(0, 6))
        self._video_import_btn = ctk.CTkButton(
            parent, text="Import videos", width=140, height=32, command=self._do_resolve,
        )
        self._video_import_btn.pack(anchor="e", padx=10, pady=(0, 10))

    # ------------------------- Music tab ------------------------------------

    def _create_music_option_vars(self) -> None:
        """BooleanVars shared between the Music tab and Settings."""
        if getattr(self, "_music_option_vars_ready", False):
            return
        self._music_option_vars_ready = True

        self.music_lyrics_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_download_lyrics")),
        )
        self.music_prefer_audio_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_prefer_audio")),
        )
        self.music_allow_explicit_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_allow_explicit", True)),
        )
        self.music_search_audio_only_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_search_audio_only")),
        )
        self.music_use_youtube_music_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_use_youtube_music")),
        )
        self.music_include_albums_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_include_albums", True)),
        )
        self.music_include_playlists_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_include_playlists", False)),
        )
        self.music_skip_duplicates_var = ctk.BooleanVar(
            value=bool(self.settings.get("music_skip_duplicates")),
        )
        from .match_config import match_quality_choices

        _quality_labels = {k: v for k, v in match_quality_choices()}
        _quality_key = str(self.settings.get("music_match_quality") or "balanced")
        self.music_match_quality_var = ctk.StringVar(
            value=_quality_labels.get(_quality_key, _quality_labels["balanced"]),
        )
        if sys.platform == "darwin":
            self.music_add_to_apple_music_var = ctk.BooleanVar(
                value=bool(self.settings.get("music_add_to_apple_music")),
            )
            self.music_apple_music_only_var = ctk.BooleanVar(
                value=bool(self.settings.get("music_apple_music_only")),
            )
            if self.music_apple_music_only_var.get():
                self.music_add_to_apple_music_var.set(True)

    def _music_search_job_params(self) -> dict:
        use_ytm = bool(self.music_use_youtube_music_var.get())
        return {
            "use_youtube_music": use_ytm,
            "audio_only": (
                False if use_ytm
                else bool(self.music_search_audio_only_var.get())
            ),
            "include_albums": bool(self.music_include_albums_var.get()) if use_ytm else False,
            "include_playlists": bool(self.music_include_playlists_var.get()) if use_ytm else False,
            "album_limit": max(1, int(self.settings.get("music_album_search_limit") or 5)),
            "match_quality": self._match_quality_key(),
            "allow_explicit": bool(self.music_allow_explicit_var.get()),
        }

    def _match_quality_key(self) -> str:
        from .match_config import match_quality_choices

        label = self.music_match_quality_var.get()
        label_to_key = {v: k for k, v in match_quality_choices()}
        if label in label_to_key:
            return label_to_key[label]
        return str(self.settings.get("music_match_quality") or "balanced")

    def _on_music_match_quality_change(self, value: str) -> None:
        from .match_config import match_quality_choices

        label_to_key = {v: k for k, v in match_quality_choices()}
        key = label_to_key.get(value, "balanced")
        self.settings.set("music_match_quality", key)

    def _on_music_use_youtube_music_change(self) -> None:
        self.settings.set(
            "music_use_youtube_music", self.music_use_youtube_music_var.get(),
        )
        self._update_music_search_audio_state()
        self._update_music_collection_search_state()

    def _update_music_search_audio_state(self) -> None:
        cb = getattr(self, "_music_search_audio_only_cb", None)
        if cb is None:
            return
        if self.music_use_youtube_music_var.get():
            cb.configure(state="disabled")
        else:
            cb.configure(state="normal")

    def _update_music_collection_search_state(self) -> None:
        """Enable/disable album/playlist search toggles based on YTM mode."""
        for attr in ("_music_include_albums_cb", "_music_include_playlists_cb"):
            cb = getattr(self, attr, None)
            if cb is None:
                continue
            cb.configure(
                state="normal" if self.music_use_youtube_music_var.get() else "disabled",
            )

    def _build_music_settings_options(self, parent: ctk.CTkFrame) -> None:
        """Music download/search options shown on the Settings tab."""
        if getattr(self, "_music_settings_options_built", False):
            return
        self._music_settings_options_built = True
        self._create_music_option_vars()

        def opt_row(**kwargs) -> ctk.CTkFrame:
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=4)
            return row

        row = opt_row()
        ctk.CTkCheckBox(
            row,
            text="Download lyrics",
            variable=self.music_lyrics_var,
            command=lambda: self.settings.set(
                "music_download_lyrics", self.music_lyrics_var.get(),
            ),
        ).pack(anchor="w")

        row = opt_row()
        ctk.CTkCheckBox(
            row,
            text="Prefer audio when downloading regular YouTube links",
            variable=self.music_prefer_audio_var,
            command=lambda: self.settings.set(
                "music_prefer_audio", self.music_prefer_audio_var.get(),
            ),
        ).pack(anchor="w")

        row = opt_row()
        ctk.CTkCheckBox(
            row,
            text="Allow explicit audio (prefer explicit over clean versions)",
            variable=self.music_allow_explicit_var,
            command=lambda: self.settings.set(
                "music_allow_explicit", self.music_allow_explicit_var.get(),
            ),
        ).pack(anchor="w")

        row = opt_row()
        self._music_search_audio_only_cb = ctk.CTkCheckBox(
            row,
            text="Prefer audio in search (regular YouTube only)",
            variable=self.music_search_audio_only_var,
            command=lambda: self.settings.set(
                "music_search_audio_only", self.music_search_audio_only_var.get(),
            ),
        )
        self._music_search_audio_only_cb.pack(anchor="w")

        row = opt_row()
        ctk.CTkLabel(row, text="Match quality:", width=120, anchor="w").pack(
            side="left", padx=(0, 8),
        )
        from .match_config import match_quality_choices

        quality_labels = {k: v for k, v in match_quality_choices()}
        ctk.CTkOptionMenu(
            row,
            values=[quality_labels[k] for k, _ in match_quality_choices()],
            variable=self.music_match_quality_var,
            width=200,
            command=self._on_music_match_quality_change,
        ).pack(side="left")
        # variable already holds the display label from _create_music_option_vars

        if sys.platform == "darwin":
            row = opt_row()
            self._music_apple_music_only_cb = ctk.CTkCheckBox(
                row,
                text="Apple Music only (delete MP3 after import)",
                variable=self.music_apple_music_only_var,
                command=self._on_apple_music_only_change,
            )
            self._music_apple_music_only_cb.pack(anchor="w")
            if not self.music_add_to_apple_music_var.get():
                self._music_apple_music_only_cb.configure(state="disabled")

        ctk.CTkLabel(
            parent,
            text=("Lyrics and prefer-audio options apply when downloading. "
                  "Allow explicit prefers labeled explicit uploads and iTunes "
                  "catalog entries over clean / radio-edit versions (on by "
                  "default). Turn it off to prefer clean versions instead. "
                  "Search prefer-audio is disabled while YouTube Music search "
                  "is on (Music tab). Album/playlist search toggles live on "
                  "the Music tab. Match quality controls playlist speed vs "
                  "accuracy — Fast reduces YouTube requests on large playlists."),
            text_color=MUTED, wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 8), anchor="w")

        self._update_music_search_audio_state()
        self._update_music_collection_search_state()

    def _build_music_tab(self, parent) -> None:
        self._create_music_option_vars()
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=0)  # options bar
        parent.grid_rowconfigure(1, weight=0)  # source picker
        parent.grid_rowconfigure(2, weight=1)  # results

        self._music_options_collapsed = bool(
            self.settings.get("music_options_collapsed", True),
        )
        opts_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        opts_wrap.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        opts_bar = ctk.CTkFrame(opts_wrap, fg_color="transparent")
        opts_bar.pack(fill="x")
        ctk.CTkLabel(
            opts_bar, text="Music", anchor="w",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).pack(side="left", padx=4)
        self._music_options_toggle_btn = ctk.CTkButton(
            opts_bar, text="Options…", width=120,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._toggle_music_options,
        )
        self._music_options_toggle_btn.pack(side="right", padx=2)

        self._music_options_body = ctk.CTkFrame(opts_wrap)
        opts_top = ctk.CTkFrame(self._music_options_body, fg_color="transparent")
        opts_top.pack(fill="x", padx=6, pady=(4, 2))
        ctk.CTkCheckBox(
            opts_top,
            text="Use YouTube Music",
            variable=self.music_use_youtube_music_var,
            command=self._on_music_use_youtube_music_change,
        ).pack(side="left", padx=8, pady=4)

        ctk.CTkCheckBox(
            opts_top,
            text="Skip duplicates",
            variable=self.music_skip_duplicates_var,
            command=lambda: self.settings.set(
                "music_skip_duplicates", self.music_skip_duplicates_var.get(),
            ),
        ).pack(side="left", padx=8, pady=4)

        if sys.platform == "darwin":
            ctk.CTkCheckBox(
                opts_top,
                text="Add to Apple Music",
                variable=self.music_add_to_apple_music_var,
                command=self._on_add_to_apple_music_change,
            ).pack(side="left", padx=8, pady=4)

        ctk.CTkButton(
            opts_top, text="Music settings…", width=130,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=lambda: self.tabs.set("Settings"),
        ).pack(side="right", padx=6, pady=4)

        opts_search = ctk.CTkFrame(self._music_options_body, fg_color="transparent")
        opts_search.pack(fill="x", padx=6, pady=(0, 6))
        self._music_include_albums_cb = ctk.CTkCheckBox(
            opts_search,
            text="Include albums in search",
            variable=self.music_include_albums_var,
            command=lambda: self.settings.set(
                "music_include_albums", self.music_include_albums_var.get(),
            ),
        )
        self._music_include_albums_cb.pack(side="left", padx=(0, 12))
        self._music_include_playlists_cb = ctk.CTkCheckBox(
            opts_search,
            text="Include playlists in search",
            variable=self.music_include_playlists_var,
            command=lambda: self.settings.set(
                "music_include_playlists", self.music_include_playlists_var.get(),
            ),
        )
        self._music_include_playlists_cb.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(
            opts_search,
            text="(YouTube Music search only)",
            text_color=MUTED,
        ).pack(side="left")
        self._update_music_collection_search_state()

        if self._music_options_collapsed:
            self._set_music_options_collapsed(True)
        else:
            self._music_options_body.pack(fill="x", pady=(2, 0))

        input_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        input_wrap.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
        self._music_input_wrap = input_wrap

        # Compact context strip shown after resolve/search (Stage B).
        self._music_context_strip = ctk.CTkFrame(input_wrap, fg_color="transparent")
        self._music_context_label = ctk.CTkLabel(
            self._music_context_strip,
            text="",
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
        )
        self._music_context_label.pack(side="left", fill="x", expand=True, padx=4)
        self._music_context_new_btn = ctk.CTkButton(
            self._music_context_strip, text="Import links…", width=90, height=28,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._music_new_link,
        )
        self._music_context_new_btn.pack(side="right", padx=2)
        self._music_context_match_btn = ctk.CTkButton(
            self._music_context_strip, text="Match on YouTube", width=140, height=28,
            command=self._music_match_all,
        )
        # packed conditionally when pending matches exist
        self._music_context_search_btn = ctk.CTkButton(
            self._music_context_strip, text="New search", width=100, height=28,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._music_new_search,
        )

        self.music_source_tabs = SourcePanel(input_wrap)
        self.music_source_tabs.pack(fill="x")
        self._build_music_search_subtab(self.music_source_tabs.add("Search YouTube"))
        self._build_music_paste_subtab(self.music_source_tabs.add("Paste Link"))
        last = self.settings.get("music_source_tab") or "search"
        self.music_source_tabs.set(
            "Search YouTube" if last == "search" else "Paste Link",
        )

        res_outer = ctk.CTkFrame(parent, fg_color=_RESULTS_PANEL_COLOR, border_width=0)
        res_outer.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 8))
        res_outer.grid_columnconfigure(0, weight=1)
        res_outer.grid_rowconfigure(1, weight=1)
        res_outer.grid_rowconfigure(2, weight=0)
        self._music_res_outer = res_outer

        header = ctk.CTkFrame(res_outer, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self._music_results_header = header
        self.music_results_header_label = ctk.CTkLabel(
            header, text="Results",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        )
        self.music_results_header_label.pack(side="left", padx=4)
        self._music_header_actions: dict[str, Any] = {}
        ctk.CTkButton(header, text="Clear", width=70,
                      fg_color="transparent", text_color=MUTED, hover_color=HOVER,
                      command=self._music_clear_results).pack(side="right", padx=2)
        self._music_more_btn = ctk.CTkButton(
            header, text="⋯", width=36,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._show_music_more_menu,
        )
        self._music_more_btn.pack(side="right", padx=2)
        self._music_download_all_btn = ctk.CTkButton(
            header, text="Download all", width=150, height=32, command=self._music_primary_action,
        )
        self._music_download_all_btn.pack(side="right", padx=2)
        from .playlist_destination import PlaylistDestination
        self.music_playlist_destination = PlaylistDestination(header, self)
        self.music_playlist_destination.pack(side="right", padx=8)

        self._music_results_body = ctk.CTkFrame(res_outer, fg_color="transparent")
        self._music_results_body.grid(row=1, column=0, sticky="nsew", padx=6, pady=0)
        self._music_results_body.grid_columnconfigure(0, weight=1)
        self._music_results_body.grid_rowconfigure(0, weight=1)

        self.music_results_frame = ctk.CTkScrollableFrame(self._music_results_body)
        self.music_results_frame.pack(fill="both", expand=True)
        self._bind_results_scroll_resize(self._music_results_body, self.music_results_frame)
        self.music_results_footer = ctk.CTkFrame(
            res_outer, fg_color="transparent", height=0,
        )
        self.music_results_footer.grid(row=2, column=0, sticky="ew",
                                       padx=6, pady=(0, 6))
        self._bind_results_pagination_watch(self.music_results_frame)
        self._music_empty = self._make_empty_state(self._music_results_body, "Find music", "Search for a song, artist, or album.\nOr paste a YouTube or Spotify link to get started.")
        self._music_render_results()
        self._music_update_input_stage()

    def _build_music_search_subtab(self, parent) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(4, 2))

        self.music_search_var = ctk.StringVar(
            value=self.settings.get("music_search_query"),
        )
        entry = SearchField(
            row, textvariable=self.music_search_var,
            height=36, placeholder_text="Search music or paste a YouTube / Spotify link",
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.music_search_entry = entry
        entry.bind("<Return>", lambda _e: self._submit_music_input())

        self._music_submit_btn = ctk.CTkButton(row, text="Search", width=110,
                                              height=36, command=self._submit_music_input)
        self._music_submit_btn.pack(side="left", padx=2)
        self.music_search_var.trace_add("write", lambda *_args: self._refresh_actions())

        ctk.CTkLabel(row, text="Show:").pack(side="left", padx=(8, 2))
        self.music_limit_var = ctk.StringVar(
            value=str(self.settings.get("music_search_limit") or 20),
        )
        ctk.CTkOptionMenu(
            row, values=["10", "20", "50"],
            variable=self.music_limit_var, width=70,
            command=self._on_music_limit_change,
        ).pack(side="left", padx=2)

    def _build_music_paste_subtab(self, parent) -> None:
        platform = self.settings.get("music_paste_platform") or "youtube"
        if platform not in PLATFORM_CONFIGS:
            platform = "youtube"
        self._music_paste_platform = platform

        ctk.CTkLabel(
            parent,
            text="Paste YouTube or Spotify links, one per line. Use one service per import.",
            anchor="w", text_color=MUTED,
        ).pack(fill="x", padx=10, pady=(8, 2))

        self.music_paste_box = ctk.CTkTextbox(parent, height=55)
        self.music_paste_box.pack(fill="x", padx=10, pady=4)
        self.music_paste_box.insert("1.0", self.settings.get("music_paste_urls") or "")

        self._music_import_feedback = ctk.CTkLabel(
            parent, text="Import the links above into the results list. Nothing downloads until you choose Download.",
            anchor="w", text_color=MUTED, wraplength=680, justify="left",
        )
        self._music_import_feedback.pack(fill="x", padx=12, pady=(0, 6))
        self._music_import_btn = ctk.CTkButton(
            parent, text="Import tracks", width=140, height=32,
            command=self._music_do_resolve,
        )
        self._music_import_btn.pack(anchor="e", padx=10, pady=(0, 10))

    def _music_platform_id(self) -> str:
        platform = getattr(self, "_music_paste_platform", None) or (
            self.settings.get("music_paste_platform") or "youtube"
        )
        if platform not in PLATFORM_CONFIGS:
            return "youtube"
        return platform

    def _set_music_paste_platform(self, platform: str) -> None:
        if platform not in PLATFORM_CONFIGS:
            platform = "youtube"
        self._music_paste_platform = platform
        self.settings.set("music_paste_platform", platform)

    # ------------------------- Options collapse (Download / Music) ----------

    def _set_download_options_collapsed(self, collapsed: bool) -> None:
        self._download_options_collapsed = collapsed
        if collapsed:
            self._download_options_body.pack_forget()
            self._download_options_toggle_btn.configure(text="Options…")
        else:
            self._download_options_body.pack(fill="x", pady=(2, 0))
            self._download_options_toggle_btn.configure(text="Hide options")
        self.settings.set("download_options_collapsed", collapsed)
        self._schedule_results_scroll_height_sync()

    def _toggle_download_options(self) -> None:
        self._set_download_options_collapsed(not self._download_options_collapsed)

    def _set_music_options_collapsed(self, collapsed: bool) -> None:
        self._music_options_collapsed = collapsed
        if collapsed:
            self._music_options_body.pack_forget()
            self._music_options_toggle_btn.configure(text="Options…")
        else:
            self._music_options_body.pack(fill="x", pady=(2, 0))
            self._music_options_toggle_btn.configure(text="Hide options")
        self.settings.set("music_options_collapsed", collapsed)
        self._schedule_results_scroll_height_sync()

    def _toggle_music_options(self) -> None:
        self._set_music_options_collapsed(not self._music_options_collapsed)

    def _show_popup_menu(
        self, button: ctk.CTkButton, items: list[tuple[str, Callable[[], None]]],
    ) -> None:
        import tkinter as tk

        if not items:
            return
        menu = tk.Menu(self, tearoff=0)
        def populate(target, entries):
            for label, command in entries:
                if isinstance(command, list):
                    child = tk.Menu(target, tearoff=0)
                    populate(child, command)
                    target.add_cascade(label=label, menu=child)
                else:
                    target.add_command(label=label, command=command, state='disabled' if command is None else 'normal')
        populate(menu, items)
        try:
            x = button.winfo_rootx()
            y = button.winfo_rooty() + button.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _show_playlist_download_menu(self, button, items, download):
        if sys.platform != 'darwin':
            self._show_popup_menu(button, items)
            return
        from . import playlists
        cached = getattr(self, '_download_playlists', None)
        if cached is None:
            self._set_status('Loading Apple Music playlists…')
            events = queue.Queue()
            def load():
                try:
                    events.put((playlists.list_playlists(threading.Event()), None))
                except Exception as error:
                    events.put((None, str(error)))
            threading.Thread(target=load, daemon=True, name='playlist-menu').start()
            def poll():
                try:
                    result, error = events.get_nowait()
                except queue.Empty:
                    self.after(100, poll)
                    return
                if error:
                    self._set_status(error)
                    if button.winfo_exists():
                        self._show_popup_menu(button, items)
                    return
                self._download_playlists = result
                if button.winfo_exists():
                    self._show_playlist_download_menu(button, items, download)
            self.after(100, poll)
            return
        targets = [(p.display_name, lambda target=p: download(target)) for p in cached
                   if not p.special_kind and not p.is_smart and not p.id.startswith('xml:')]
        def refresh():
            self._download_playlists = None
            self._show_playlist_download_menu(button, items, download)
        targets = targets or [('No writable playlists found', None)]
        targets.append(('Refresh playlists…', refresh))
        self._show_popup_menu(button, [('Download to playlist', targets), *items])

    def _download_to_playlist(self, playlist, *, result=None, track=None):
        out_dir = self.settings.get('music_dir') or _pick_folder()
        if not out_dir:
            return None
        self.settings.set('music_dir', out_dir)
        if track is not None and not track.youtube_url:
            from dataclasses import replace
            track = replace(track, match_status=MATCH_PENDING)
        label = track.display_title(60) if track is not None else result.display_title(60)
        job = self._enqueue_music_download(
            track.youtube_url if track is not None else result.url,
            f'Music → {playlist.name}: {label}', out_dir=out_dir,
            cookies=self.settings.get('cookies_path') or None, result=result, track=track,
            playlist_id=playlist.id)
        self._set_status(f'Queued for {playlist.name}: {label}')
        return job

    def _download_all_to_playlist(self, playlist):
        if self._music_showing_tracks:
            for track in tuple(self.music_tracks):
                if self._download_to_playlist(playlist, track=track) is None:
                    break
        else:
            for result in tuple(self.music_results):
                if result.kind == 'track' and self._download_to_playlist(playlist, result=result) is None:
                    break

    def _show_download_more_menu(self) -> None:
        self._show_popup_menu(self._download_more_btn, [
            ("Download to folder…", lambda: self._download_all(override=True)),
        ])

    def _show_music_more_menu(self) -> None:
        items: list[tuple[str, Callable[[], None]]] = []
        actions = self._music_header_actions
        if actions.get("match"):
            items.append(("Match on YouTube", self._music_match_all))
        if actions.get("retry"):
            items.append((
                actions.get("retry_label", "Retry failed"),
                self._music_retry_all_failed,
            ))
        if actions.get("review"):
            items.append(("Review matches", self._music_review_matches))
        items.append(("Download to folder…", lambda: self._music_download_all(override=True)))
        items.append(("Import links…", self._music_new_link))
        self._show_playlist_download_menu(self._music_more_btn, items, self._download_all_to_playlist)

    def _sync_results_scroll_frame_height(self, body, frame) -> None:
        """Resize a CTkScrollableFrame viewport to fill its grid body."""
        if self._syncing_scroll_height:
            return
        self._syncing_scroll_height = True
        try:
            # Avoid update_idletasks here — it re-enters Configure handlers and
            # has caused RecursionError / multi-second freezes on macOS.
            h = int(body.winfo_height())
            if h < 40:
                return
            if abs(int(frame.cget("height")) - h) > 4:
                frame.configure(height=h)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._syncing_scroll_height = False

    def _sync_all_results_scroll_heights(self) -> None:
        download_body = getattr(self, "_download_results_body", None)
        download_frame = getattr(self, "results_frame", None)
        if download_body is not None and download_frame is not None:
            self._sync_results_scroll_frame_height(download_body, download_frame)
        music_body = getattr(self, "_music_results_body", None)
        music_frame = getattr(self, "music_results_frame", None)
        if music_body is not None and music_frame is not None:
            self._sync_results_scroll_frame_height(music_body, music_frame)

    def _schedule_results_scroll_height_sync(self) -> None:
        """Re-sync after layout settles (tab switch, collapse, new rows)."""
        self.after(1, self._sync_all_results_scroll_heights)
        self.after(120, self._sync_all_results_scroll_heights)

    def _bind_results_scroll_resize(self, body, frame) -> None:
        """Keep CTkScrollableFrame canvas sized to its grid-allocated body."""
        key = id(body)

        def _schedule_sync(_event=None) -> None:
            if self._syncing_scroll_height:
                return
            job = self._results_scroll_sync_pending.get(key)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
            self._results_scroll_sync_pending[key] = self.after(
                50,
                lambda b=body, f=frame: self._sync_results_scroll_frame_height(b, f),
            )

        body.bind("<Configure>", _schedule_sync)
        _schedule_sync()

    def _on_main_tab_changed(self) -> None:
        try:
            tab_name = self.tabs.get()
            tab_key = _MAIN_TAB_KEYS.get(tab_name)
            if tab_key:
                self.settings.set("main_tab", tab_key)
        except Exception:  # noqa: BLE001
            pass
        self._schedule_results_scroll_height_sync()

    # ------------------------- Embed thumbnail (legacy) ---------------------

    def _build_embed_thumbnail_ui(self, parent) -> None:
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
            anchor="w", text_color=MUTED,
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
        ctk.CTkLabel(parent, text="Settings", anchor="w",
                     font=ctk.CTkFont(size=28, weight="bold")).pack(fill="x", padx=12, pady=(6, 12))
        scroll = ctk.CTkScrollableFrame(parent)
        scroll.pack(fill="both", expand=True, padx=8, pady=8)
        self._settings_scroll_frame = scroll

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
            text_color=MUTED, wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 4), anchor="w")
        self._build_music_settings_options(s_music)

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
            text_color=MUTED, wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 8), anchor="w")

        s_debug = section("Debug")
        row = ctk.CTkFrame(s_debug, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 8))
        self.verbose_var = ctk.BooleanVar(value=bool(self.settings.get("verbose")))

        def on_verbose() -> None:
            self.settings.set("verbose", bool(self.verbose_var.get()))

        ctk.CTkCheckBox(
            row,
            text="Verbose logging (shows yt-dlp debug output)",
            variable=self.verbose_var,
            command=on_verbose,
        ).pack(anchor="w")

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

        reduce_var = ctk.BooleanVar(value=bool(self.settings.get("reduce_motion")))
        ctk.CTkCheckBox(
            s_appear, text="Reduce motion", variable=reduce_var,
            command=lambda: self.settings.set("reduce_motion", reduce_var.get()),
        ).pack(anchor="w", padx=14, pady=(0, 12))

        scroll_row = ctk.CTkFrame(s_appear, fg_color="transparent")
        scroll_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(scroll_row, text="Scroll direction:", width=220,
                     anchor="w").pack(side="left")
        scroll_var = ctk.StringVar(
            value=self.settings.get("scroll_direction") or "auto"
        )

        def on_scroll_dir(_value=None) -> None:
            v = scroll_var.get()
            self.settings.set("scroll_direction", v)
            self._scroll_sign = _detect_scroll_sign(self.settings)

        for value, label in (("auto", "Auto (follow system)"),
                             ("natural", "Natural"),
                             ("inverted", "Inverted")):
            ctk.CTkRadioButton(scroll_row, text=label, variable=scroll_var,
                               value=value, command=on_scroll_dir).pack(
                side="left", padx=6,
            )

        # Legacy tools (formerly top-level tabs)
        s_legacy = section("Legacy")
        ctk.CTkLabel(
            s_legacy,
            text="Embed Thumbnail — older workflow for attaching cover art to "
                 "existing audio files. Prefer Music mode for new downloads.",
            text_color=MUTED, wraplength=820, justify="left",
        ).pack(fill="x", padx=14, pady=(8, 2), anchor="w")
        self._build_embed_thumbnail_ui(s_legacy)

        # About
        s_about = section("About")
        ff = find_ffmpeg()
        try:
            import yt_dlp  # noqa: WPS433
            ytv = yt_dlp.version.__version__
        except Exception:  # noqa: BLE001
            ytv = "unknown"
        about_lines = [
            f"easy-dlp   {__version__}",
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
                      fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER).pack(
            fill="x", padx=10, pady=(10, 10),
        )

    # ------------------------- bottom panels --------------------------------

    # ---- Heights used by the activity dock.
    _ACTIVE_EXPANDED_H = 150
    _RECENT_EXPANDED_H = 110
    _COLLAPSED_H = 64

    _PANEL_HEIGHTS = {"normal": None, "large": None, "xlarge": None}

    def _panel_height_map(self) -> dict[str, int]:
        # Derive heights from the log mapping for consistent UI feel.
        return {"normal": 130, "large": 280, "xlarge": 450}

    def _active_expanded_height(self) -> int:
        key = str(self.settings.get("panel_active_height") or "normal")
        heights = self._panel_height_map()
        return heights.get(key, self._ACTIVE_EXPANDED_H)

    def _recent_expanded_height(self) -> int:
        key = str(self.settings.get("panel_recent_height") or "normal")
        heights = self._panel_height_map()
        return heights.get(key, self._RECENT_EXPANDED_H)

    def _log_expanded_height(self) -> int:
        return _LOG_HEIGHTS.get(self._log_height, _LOG_HEIGHTS["normal"])

    def _activity_expanded_height(self) -> int:
        seg = getattr(self, "_activity_segment", "active")
        if seg == "recent":
            return self._recent_expanded_height()
        if seg == "log":
            return self._log_expanded_height()
        return self._active_expanded_height()

    def _build_activity_dock(self) -> None:
        """Single bottom dock with Active | Recent | Log segments."""
        outer = ctk.CTkFrame(self, height=self._COLLAPSED_H, fg_color=PANEL, corner_radius=0)
        outer.pack(side="bottom", fill="x")
        outer.pack_propagate(False)
        self._activity_dock = outer
        # Aliases kept for any remaining references / popout restore.
        self._active_outer = outer
        self._recent_outer = outer
        self._log_outer = outer

        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(8, 4))
        self._activity_header = header

        seg_row = ctk.CTkFrame(header, fg_color="transparent")
        seg_row.pack(side="left")
        self._activity_seg_btns: dict[str, ctk.CTkButton] = {}
        for key, label in (
            ("active", "Active"),
            ("recent", "Recent"),
            ("log", "Log"),
        ):
            btn = ctk.CTkButton(
                seg_row, text=label, width=78, height=24,
                fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
                command=lambda k=key: self._on_activity_segment_click(k),
            )
            btn.pack(side="left", padx=1)
            self._activity_seg_btns[key] = btn

        # Compatibility labels updated by existing header helpers.
        self.active_header = self._activity_seg_btns["active"]
        self.recent_header = self._activity_seg_btns["recent"]

        self.status_var = ctk.StringVar(value="Ready when you are")
        ctk.CTkLabel(header, textvariable=self.status_var, anchor="w", width=100, text_color=MUTED).pack(
            side="left", fill="x", expand=True, padx=(8, 4),
        )

        self._activity_actions = ctk.CTkFrame(header, fg_color="transparent")
        self._activity_actions.pack(side="right")

        self._activity_toggle_btn = ctk.CTkButton(
            header, text="Show ▸", width=72, height=28,
            fg_color="transparent", text_color=TEXT, hover_color=HOVER,
            command=self._toggle_activity_dock,
        )
        self._activity_toggle_btn.pack(side="right", padx=(2, 0))

        # Shared action widgets (shown/hidden per segment).
        self._active_size_btn = ctk.CTkButton(
            self._activity_actions,
            text=str(self.settings.get("panel_active_height") or "normal").capitalize(),
            width=80, height=24, fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._cycle_active_height,
        )
        self._active_popout_btn = ctk.CTkButton(
            self._activity_actions, text="Pop out", width=72, height=24,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._toggle_active_popout,
        )
        self._active_cancel_btn = ctk.CTkButton(
            self._activity_actions, text="Cancel all", width=100, height=24,
            command=self.jobs.cancel_all,
        )
        self._recent_size_btn = ctk.CTkButton(
            self._activity_actions,
            text=str(self.settings.get("panel_recent_height") or "normal").capitalize(),
            width=80, height=24, fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._cycle_recent_height,
        )
        self._recent_popout_btn = ctk.CTkButton(
            self._activity_actions, text="Pop out", width=72, height=24,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._toggle_recent_popout,
        )
        self._recent_clear_btn = ctk.CTkButton(
            self._activity_actions, text="Clear", width=64, height=24,
            command=self._clear_recent,
        )
        self._recent_retry_all_btn = ctk.CTkButton(
            self._activity_actions, text="Retry all failed", width=120, height=24,
            fg_color="#8b2e2e", hover_color="#a33",
            command=self._retry_all_failed_recent,
        )
        self._log_latest_btn = ctk.CTkButton(
            self._activity_actions, text="↓ Latest", width=72, height=24,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._log_jump_to_latest,
        )
        self._log_size_btn = ctk.CTkButton(
            self._activity_actions,
            text=_LOG_HEIGHT_LABELS.get(self._log_height, "Size"),
            width=100, height=24, fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._cycle_log_height,
        )
        self._log_popout_btn = ctk.CTkButton(
            self._activity_actions, text="Pop out", width=72, height=24,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._toggle_log_popout,
        )
        self._log_clear_btn = ctk.CTkButton(
            self._activity_actions, text="Clear", width=64, height=24,
            command=self._clear_log,
        )

        self._summary_progress = SmoothProgress(
            outer, height=3, corner_radius=0,
            reduced_motion=lambda: bool(self.settings.get("reduce_motion")),
        )
        self._summary_progress.pack(fill="x", padx=16, pady=(0, 6))
        self._summary_jobs: dict[int, tuple[str, float]] = {}

        # Bodies live in one container; only the selected segment is packed.
        self._activity_body = ctk.CTkFrame(outer, fg_color="transparent")
        self.active_frame = ctk.CTkScrollableFrame(self._activity_body)
        self.recent_frame = ctk.CTkScrollableFrame(self._activity_body)
        log_font = ctk.CTkFont(size=13 if self._log_height != "normal" else 12)
        self.log_box = ctk.CTkTextbox(
            self._activity_body, height=80, wrap="none", font=log_font,
        )
        self.log_box.configure(state="disabled")
        self._bind_log_scroll_tracking(self.log_box)

        # Legacy per-panel collapse flags (settings + ensure_active_expanded).
        self._active_collapsed = True
        self._recent_collapsed = True
        self._log_collapsed = True

        seg = str(self.settings.get("activity_dock_segment") or "active")
        if seg not in ("active", "recent", "log"):
            seg = "active"
        # Prefer an expanded segment from prior settings if any.
        if not bool(self.settings.get("panel_active_collapsed", True)):
            seg = "active"
            self._activity_collapsed = False
        elif not bool(self.settings.get("panel_recent_collapsed", True)):
            seg = "recent"
            self._activity_collapsed = False
        elif not bool(self.settings.get("panel_log_collapsed", True)):
            seg = "log"
            self._activity_collapsed = False
        else:
            self._activity_collapsed = True
        self._activity_segment = seg

        self._apply_activity_dock_layout()
        self._update_activity_segment_styles()
        self.active_header.configure(text="Active (0)")
        self.recent_header.configure(text="Recent (0)")

    def _on_activity_segment_click(self, segment: str) -> None:
        if self._activity_segment == segment and not self._activity_collapsed:
            # Clicking the open segment collapses the dock.
            self._toggle_activity_dock()
            return
        self._activity_segment = segment
        self.settings.set("activity_dock_segment", segment)
        if self._activity_collapsed:
            self._activity_collapsed = False
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()

    def _toggle_activity_dock(self) -> None:
        self._activity_collapsed = not self._activity_collapsed
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()
        self._schedule_results_scroll_height_sync()

    def _persist_activity_collapse_flags(self) -> None:
        collapsed = self._activity_collapsed
        seg = self._activity_segment
        self._active_collapsed = collapsed or seg != "active"
        self._recent_collapsed = collapsed or seg != "recent"
        self._log_collapsed = collapsed or seg != "log"
        self.settings.set("panel_active_collapsed", self._active_collapsed)
        self.settings.set("panel_recent_collapsed", self._recent_collapsed)
        self.settings.set("panel_log_collapsed", self._log_collapsed)
        self.settings.set("activity_dock_segment", seg)

    def _apply_activity_dock_layout(self) -> None:
        for child in self._activity_body.winfo_children():
            child.pack_forget()
        self._activity_body.pack_forget()

        for child in self._activity_actions.winfo_children():
            child.pack_forget()

        if self._activity_collapsed:
            self._activity_dock.configure(height=self._COLLAPSED_H)
            self._activity_toggle_btn.configure(text="Show ▸")
            self._update_activity_segment_styles()
            return

        self._activity_dock.configure(height=self._activity_expanded_height())
        self._activity_toggle_btn.configure(text="Hide ▾")
        self._activity_body.pack(fill="both", expand=True, padx=6, pady=(0, 4))

        seg = self._activity_segment
        if seg == "active":
            self.active_frame.pack(fill="both", expand=True)
            self._active_cancel_btn.pack(side="right", padx=2)
            self._active_popout_btn.pack(side="right", padx=2)
            self._active_size_btn.pack(side="right", padx=2)
        elif seg == "recent":
            self.recent_frame.pack(fill="both", expand=True)
            self._recent_clear_btn.pack(side="right", padx=2)
            self._recent_popout_btn.pack(side="right", padx=2)
            self._recent_size_btn.pack(side="right", padx=2)
            self._update_recent_header()  # may show retry btn
        else:
            self.log_box.pack(fill="both", expand=True)
            self._log_clear_btn.pack(side="right", padx=2)
            self._log_popout_btn.pack(side="right", padx=2)
            self._log_size_btn.pack(side="right", padx=2)
            self.after_idle(self._update_log_latest_btn)

        self._update_activity_segment_styles()

    def _update_activity_segment_styles(self) -> None:
        for key, btn in self._activity_seg_btns.items():
            if key == self._activity_segment and not self._activity_collapsed:
                btn.configure(fg_color=HOVER, text_color=VIOLET, border_width=0)
            else:
                btn.configure(fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER)

    def _open_activity(self, segment: str) -> None:
        self._activity_segment = segment
        self._activity_collapsed = False
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()
        self._schedule_results_scroll_height_sync()

    def _open_media_link(self, url: str) -> None:
        """Hand listening to the browser without overlapping an app preview."""
        button = getattr(self, "_preview_button", None)
        if button is not None:
            button._toggle()
        webbrowser.open(url)

    def _focus_search(self, _event=None):
        if self.tabs.get() == "Playlists":
            self.playlists_page.focus_filter()
            return "break"
        if self.tabs.get() not in ("Music", "Video"):
            self.tabs.set("Music")
        panel = self.music_source_tabs if self.tabs.get() == "Music" else self.source_tabs
        panel.set("Search YouTube")
        entry = self.music_search_entry if self.tabs.get() == "Music" else self.search_entry
        entry.focus_set()
        entry.select_range(0, "end")
        return "break"

    def _dismiss_detail(self, _event=None):
        if self.tabs.get() == "Playlists":
            if self.playlists_page._busy:
                self.playlists_page._cancel_work()
            return "break"
        if self.tabs.get() == "Identify":
            if self.identify_page._busy:
                self.identify_page._cancel_work()
            return "break"
        if self._music_alternate_open_index is not None:
            self._music_close_rematch()
        elif not self._activity_collapsed:
            self._toggle_activity_dock()
        else:
            panel = self.music_source_tabs if self.tabs.get() == "Music" else self.source_tabs
            panel.set("Search YouTube")
        return "break"

    def _refresh_actions(self) -> None:
        active = self.jobs.active()
        lookup_kinds = {"search", "resolve", "source_resolve", "source_match_all", "search_more"}
        for context, prefix, variable_name in (
            ("music", "_music", "music_search_var"),
            ("download", "_video", "search_var"),
        ):
            busy = any(j.kind in lookup_kinds and
                       j.params.get("results_context", "download") == context for j in active)
            variable = getattr(self, variable_name, None)
            query = variable.get().strip() if variable is not None else ""
            button = getattr(self, prefix + "_submit_btn", None)
            if button is not None:
                text = "Working…" if busy else ("Preview link" if is_url(query) else "Search")
                state = "disabled" if busy or not query else "normal"
                if (button.cget("text"), button.cget("state")) != (text, state):
                    button.configure(text=text, state=state, fg_color=ACCENT_FILL if state == "normal" else HOVER)
            button = getattr(self, prefix + "_import_btn", None)
            if button is not None:
                text = "Reading links…" if busy else ("Import tracks" if context == "music" else "Import videos")
                state = "disabled" if busy else "normal"
                if (button.cget("text"), button.cget("state")) != (text, state):
                    button.configure(text=text, state=state, fg_color=ACCENT_FILL if state == "normal" else HOVER)
            button = getattr(self, prefix + "_download_all_btn", None)
            if button is None:
                continue
            if context == "music" and self._music_showing_tracks:
                pending = any(t.match_status == MATCH_PENDING for t in self.music_tracks)
                ready = sum(t.is_downloadable() for t in self.music_tracks)
                failed = any(t.match_status == MATCH_FAILED for t in self.music_tracks)
                text = "Find matches" if pending else (
                    f"Download {ready} ready" if ready else "Retry matches" if failed else "Download all")
                enabled = bool(pending or ready or failed)
            else:
                items = self.music_results if context == "music" else self.results
                count = sum(r.kind == "track" for r in items)
                text = f"Download {count}" if count else "Download all"
                enabled = bool(count)
            state = "normal" if enabled and not busy else "disabled"
            if (button.cget("text"), button.cget("state")) != (text, state):
                button.configure(text=text, state=state, fg_color=ACCENT_FILL if state == "normal" else HOVER)

    def _music_primary_action(self) -> None:
        if self._input_busy("music"):
            return
        if self._music_showing_tracks and any(t.match_status == MATCH_PENDING for t in self.music_tracks):
            self._music_match_all()
        elif self._music_showing_tracks and not any(t.is_downloadable() for t in self.music_tracks):
            self._music_retry_all_failed()
        else:
            self._music_download_all(override=False)
        self._refresh_actions()

    def _input_busy(self, context: str) -> bool:
        busy = any(
            j.kind in ("search", "resolve", "source_resolve", "source_match_all", "search_more")
            and j.params.get("results_context", "download") == context
            for j in self.jobs.active()
        )
        if busy:
            self._set_status("Still working on this search. Cancel it in Downloads to start another.")
        return busy

    def _submit_music_input(self) -> None:
        if self._input_busy("music"):
            return
        query = self.music_search_var.get().strip()
        self._music_close_rematch(render=False)
        if not self.music_results_frame.winfo_ismapped():
            self.music_results_frame.pack(fill="both", expand=True)
        if is_url(query):
            self._music_do_resolve(single_url=query)
            return
        else:
            self._music_do_search()
        if query:
            self._set_status("Looking up “" + _truncate(query, 64) + "”…")

    def _submit_video_input(self) -> None:
        if self._input_busy("download"):
            return
        query = self.search_var.get().strip()
        if is_url(query):
            self._do_resolve(single_url=query)
            return
        else:
            self._do_search()
        if query:
            self._set_status("Looking up “" + _truncate(query, 64) + "”…")

    # Legacy toggle entry points (menus / old callers).
    def _toggle_active(self) -> None:
        if self._activity_segment != "active" or self._activity_collapsed:
            self._activity_segment = "active"
            self._activity_collapsed = False
        else:
            self._activity_collapsed = True
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()

    def _toggle_recent(self) -> None:
        if self._activity_segment != "recent" or self._activity_collapsed:
            self._activity_segment = "recent"
            self._activity_collapsed = False
        else:
            self._activity_collapsed = True
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()

    def _toggle_log(self) -> None:
        if self._activity_segment != "log" or self._activity_collapsed:
            self._activity_segment = "log"
            self._activity_collapsed = False
        else:
            self._activity_collapsed = True
        self._apply_activity_dock_layout()
        self._persist_activity_collapse_flags()
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

    def _on_add_to_apple_music_change(self) -> None:
        enabled = bool(self.music_add_to_apple_music_var.get())
        self.settings.set("music_add_to_apple_music", enabled)
        if not enabled:
            self.music_apple_music_only_var.set(False)
            self.settings.set("music_apple_music_only", False)
        cb = getattr(self, "_music_apple_music_only_cb", None)
        if cb is not None:
            cb.configure(state="normal" if enabled else "disabled")

    def _on_apple_music_only_change(self) -> None:
        only = bool(self.music_apple_music_only_var.get())
        if only:
            self.music_add_to_apple_music_var.set(True)
            self.settings.set("music_add_to_apple_music", True)
            self._music_apple_music_only_cb.configure(state="normal")
        self.settings.set("music_apple_music_only", only)

    def _music_job_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "download_lyrics": bool(self.music_lyrics_var.get()),
            "prefer_audio": bool(self.music_prefer_audio_var.get()),
            "allow_explicit": bool(self.music_allow_explicit_var.get()),
            "enrich_metadata": True,
        }
        if hasattr(self, "music_add_to_apple_music_var"):
            apple_music_only = bool(self.music_apple_music_only_var.get())
            params["add_to_apple_music"] = (
                bool(self.music_add_to_apple_music_var.get()) or apple_music_only
            )
            params["apple_music_only"] = apple_music_only
        return params

    def _enqueue_music_download(
        self,
        url: str,
        label: str,
        *,
        out_dir: str,
        cookies: str | None,
        result: SearchResult | None = None,
        track: MusicTrack | None = None,
        user_picked: bool = False,
        playlist_id: str | None = None,
    ) -> Job:
        if playlist_id is None:
            selected = self.music_playlist_destination.selected()
            playlist_id = selected.id if selected else ""
        params = self._music_job_params()
        params["verbose"] = bool(self.settings.get("verbose"))
        if track is not None:
            params["expected_artist"] = track.artist
            params["expected_title"] = track.title
            params["expected_duration_s"] = track.duration_s
            params["source_url"] = track.source_url or url
            params["source_title"] = track.title
            params["source_uploader"] = track.artist
            params["source_duration_s"] = track.duration_s
            params["source_thumbnail_url"] = track.cover_url or track.thumbnail_url
            params["source_album"] = track.album
            params["source_album_artist"] = track.album_artist
            params["source_track_number"] = track.track_number
            params["source_disc_number"] = track.disc_number
            params["source_cover_url"] = track.cover_url
            # Auto-matched tracks may still rematch for Prefer audio.
        elif result is not None:
            params["source_url"] = result.url
            params["source_title"] = result.title
            params["source_uploader"] = result.uploader
            params["source_duration_s"] = result.duration_s
            params["source_thumbnail_url"] = result.thumbnail_url
            if user_picked:
                # User picked this result — don't rematch away from their choice.
                params["skip_prefer_audio_rematch"] = True
        if track is not None and not track.youtube_url and (track.source == 'shazam' or playlist_id):
            params.update(self._music_search_job_params())
            params['match_before_download'] = True
            params['tracks'] = [track.to_dict()]
            params['skip_existing'] = bool(self.music_skip_duplicates_var.get()) and not playlist_id
        if playlist_id:
            params['add_to_apple_music'] = True
            params['apple_music_playlist_id'] = playlist_id
        return self.jobs.enqueue(
            kind="music",
            label=label,
            url=url,
            output_dir=out_dir,
            cookies_path=cookies,
            **params,
        )

    def _music_skip_duplicates_checks_apple_music(self) -> bool:
        return sys.platform == "darwin"

    def _music_duplicate_check(
        self,
        out_dir: str,
        *,
        result: SearchResult | None = None,
        track: MusicTrack | None = None,
    ) -> tuple[bool, str, str]:
        return check_music_duplicate(
            out_dir,
            result=result,
            track=track,
            check_apple_music=self._music_skip_duplicates_checks_apple_music(),
        )

    def _duplicate_exists_message(self, display: str, location: str, out_dir: str) -> str:
        where = duplicate_location_label(location, out_dir)
        if location == "library":
            return f"{display} is already in {where}."
        return f"{display} already exists in {where}."

    def _ask_batch_duplicate_action(self, names: list[str], out_dir: str) -> str:
        if self._music_skip_duplicates_checks_apple_music():
            where = "your Apple Music library or output folder"
        else:
            where = Path(out_dir).name or out_dir
        preview = names[:12]
        lines = "\n".join(f"  • {n}" for n in preview)
        extra = f"\n  … and {len(names) - 12} more" if len(names) > 12 else ""
        msg = (
            f"{len(names)} track(s) already exist in {where}:\n\n"
            f"{lines}{extra}\n\n"
            "Yes — download all (including duplicates)\n"
            "No — skip duplicates, download the rest\n"
            "Cancel — abort"
        )
        choice = messagebox.askyesnocancel("Skip duplicates", msg)
        if choice is None:
            return "cancel"
        return "all" if choice else "skip"

    def _notify_skipped_duplicates(self, names: list[str]) -> None:
        if not names:
            return
        if self._music_skip_duplicates_checks_apple_music():
            where = "library or output folder"
        else:
            where = "output folder"
        self._set_status(f"Skipped {len(names)} duplicate(s) already in {where}.")

    def _maybe_enqueue_music_download(
        self,
        url: str,
        label: str,
        *,
        out_dir: str,
        cookies: str | None,
        result: SearchResult | None = None,
        track: MusicTrack | None = None,
        force: bool = False,
        user_picked: bool = False,
    ) -> bool:
        selected = self.music_playlist_destination.selected()
        playlist_id = selected.id if selected else ''
        if force or selected or not self.music_skip_duplicates_var.get():
            self._enqueue_music_download(
                url, label,
                out_dir=out_dir, cookies=cookies, result=result, track=track,
                user_picked=user_picked, playlist_id=playlist_id,
            )
            return True

        # iTunes + Apple Music checks can take seconds — never block the UI.
        self._set_status("Checking for duplicates…")

        def work() -> None:
            try:
                exists, display, location = self._music_duplicate_check(
                    out_dir, result=result, track=track,
                )
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"

                def on_err() -> None:
                    self._set_status(f"Duplicate check failed — queuing anyway ({err})")
                    self._enqueue_music_download(
                        url, label,
                        out_dir=out_dir, cookies=cookies,
                        result=result, track=track,
                        user_picked=user_picked, playlist_id=playlist_id,
                    )

                self.after(0, on_err)
                return

            def on_main() -> None:
                if exists:
                    if not messagebox.askyesno(
                        "Already downloaded",
                        f"{self._duplicate_exists_message(display, location, out_dir)}\n\n"
                        "Download anyway?",
                    ):
                        self._set_status(f"Skipped duplicate: {display}")
                        return
                self._enqueue_music_download(
                    url, label,
                    out_dir=out_dir, cookies=cookies, result=result, track=track,
                    user_picked=user_picked, playlist_id=playlist_id,
                )
                self._set_status(f"Queued: {label}")

            self.after(0, on_main)

        threading.Thread(target=work, daemon=True, name="dup-check").start()
        return True

    def _maybe_cap_playlist_parallel(self, item_count: int) -> None:
        if item_count < 5:
            return
        from .match_config import get_match_config

        cfg = get_match_config(self._match_quality_key())
        self._parallel_before_playlist = int(
            self.settings.get("max_parallel_downloads") or 2,
        )
        self.jobs.set_max_parallel(
            min(self._parallel_before_playlist, cfg.playlist_parallel),
        )

    def _maybe_restore_playlist_parallel(self) -> None:
        saved = getattr(self, "_parallel_before_playlist", None)
        if saved is None:
            return
        active_music = [j for j in self.jobs.active() if j.kind == "music"]
        if not active_music:
            self.jobs.set_max_parallel(saved)
            self._parallel_before_playlist = None

    def _enqueue_music_downloads_batch(
        self,
        out_dir: str,
        cookies: str | None,
        items: list[tuple[str, str, SearchResult | None, MusicTrack | None]],
    ) -> None:
        if not items:
            return

        # Cap parallelism on the UI thread (cheap), then do any heavy
        # duplicate / library work off-thread so the UI stays responsive.
        self._maybe_cap_playlist_parallel(len(items))

        selected = self.music_playlist_destination.selected()
        playlist_id = selected.id if selected else ''
        if selected or not self.music_skip_duplicates_var.get():
            for url, label, result, track in items:
                self._enqueue_music_download(
                    url, label,
                    out_dir=out_dir, cookies=cookies, result=result, track=track, playlist_id=playlist_id,
                )
            return

        self._set_status(f"Checking {len(items)} track(s) for duplicates…")

        def work() -> None:
            from . import apple_music as am

            library_cache = (
                self._music_skip_duplicates_checks_apple_music() and len(items) > 3
            )
            if library_cache:
                try:
                    am.begin_library_cache(
                        progress=lambda msg: self.after(0, lambda m=msg: self._set_status(m)),
                    )
                except Exception:  # noqa: BLE001
                    library_cache = False

            try:
                to_enqueue: list[
                    tuple[str, str, SearchResult | None, MusicTrack | None]
                ] = []
                skipped: list[
                    tuple[str, str, SearchResult | None, MusicTrack | None, str]
                ] = []
                for url, label, result, track in items:
                    exists, display, _location = self._music_duplicate_check(
                        out_dir, result=result, track=track,
                    )
                    if exists:
                        skipped.append((url, label, result, track, display))
                    else:
                        to_enqueue.append((url, label, result, track))
            finally:
                if library_cache:
                    am.end_library_cache()

            def on_main() -> None:
                self._finish_batch_after_dup_check(
                    out_dir, cookies, items, to_enqueue, skipped,
                )

            self.after(0, on_main)

        threading.Thread(target=work, daemon=True, name="batch-dup-check").start()

    def _finish_batch_after_dup_check(
        self,
        out_dir: str,
        cookies: str | None,
        items: list[tuple[str, str, SearchResult | None, MusicTrack | None]],
        to_enqueue: list[tuple[str, str, SearchResult | None, MusicTrack | None]],
        skipped: list[tuple[str, str, SearchResult | None, MusicTrack | None, str]],
    ) -> None:
        if not skipped:
            for url, label, result, track in items:
                self._enqueue_music_download(
                    url, label,
                    out_dir=out_dir, cookies=cookies, result=result, track=track,
                )
            return

        if self._batch_duplicate_dialog_open:
            self._set_status("Duplicate check already in progress…")
            return

        skipped_names = list(dict.fromkeys(display for *_, display in skipped))
        self._batch_duplicate_dialog_open = True
        try:
            action = self._ask_batch_duplicate_action(skipped_names, out_dir)
        finally:
            self._batch_duplicate_dialog_open = False

        if action == "cancel":
            self._set_status("Download cancelled.")
            return
        if action == "all":
            for url, label, result, track in items:
                self._enqueue_music_download(
                    url, label,
                    out_dir=out_dir, cookies=cookies, result=result, track=track,
                )
            return

        self._notify_skipped_duplicates(skipped_names)
        for url, label, result, track in to_enqueue:
            self._enqueue_music_download(
                url, label,
                out_dir=out_dir, cookies=cookies, result=result, track=track,
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
            verbose=bool(self.settings.get("verbose")),
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
        self._music_track_count = 0
        self._music_album_count = 0
        self._music_albums_exhausted = False
        self._music_search_page_size = max(10, limit)
        self._music_pending_search_query = query if not is_url(query) else None
        search_params = self._music_search_job_params()
        self._music_search_audio_only = search_params["audio_only"]
        self._music_use_youtube_music = search_params["use_youtube_music"]
        self._music_search_include_albums = search_params["include_albums"]
        self.music_tracks = []
        self._music_showing_tracks = False
        self.music_results = []
        self._music_pending_stream_albums = []
        self._music_render_token += 1  # cancel in-flight row chunks
        self.music_results_header_label.configure(text="Results — searching…")
        self._music_render_results()
        label = f"Music search: {query[:60]}"
        job = self.jobs.enqueue(
            kind="search", label=label,
            query=query, limit=limit,
            cookies_path=cookies,
            videos_only=True,
            results_context="music",
            verbose=bool(self.settings.get("verbose")),
            **search_params,
        )
        self._music_stream_job_id = job.id

    def _import_feedback(self, context: str, text: str, *, error: bool = False):
        label = self._music_import_feedback if context == "music" else self._video_import_feedback
        label.configure(text=text, text_color=("#b42318", "#ff8a80") if error else MUTED)
        self._set_status(text)
        if error:
            panel = self.music_source_tabs if context == "music" else self.source_tabs
            panel.set("Paste Link" if context == "music" else "Paste URLs")

    def _do_resolve(self, *, single_url: str | None = None) -> None:
        if self._input_busy("download"):
            return
        text = single_url if single_url is not None else self.paste_box.get("1.0", "end").strip()
        try:
            urls, _platform = parse_import_links(text)
        except ValueError as error:
            self._import_feedback("download", str(error), error=True)
            return
        if single_url is None:
            self.settings.set("paste_urls", text)
        self.settings.set("source_tab", "paste")
        self._search_query = None
        self.jobs.enqueue(
            kind="resolve", label=f"Import {len(urls)} video link(s)",
            urls=urls, cookies_path=self.settings.get("cookies_path") or None,
            import_preview=True,
        )
        self._import_feedback("download", f"Reading {len(urls)} link(s)… Downloads have not started.")
        self._refresh_actions()

    def _music_do_resolve(self, *, single_url: str | None = None) -> None:
        if self._input_busy("music"):
            return
        text = single_url if single_url is not None else self.music_paste_box.get("1.0", "end").strip()
        try:
            urls, platform = parse_import_links(text, music=True)
        except ValueError as error:
            self._import_feedback("music", str(error), error=True)
            return
        self._music_close_rematch(render=False)
        if not self.music_results_frame.winfo_ismapped():
            self.music_results_frame.pack(fill="both", expand=True)
        if single_url is None:
            self.settings.set("music_paste_urls", text)
        self.settings.set("music_source_tab", "paste")
        self._set_music_paste_platform(platform)
        self._music_auto_download = False
        self._music_search_query = None
        self._music_pending_search_query = None
        params = dict(urls=urls, cookies_path=self.settings.get("cookies_path") or None,
                      results_context="music", import_preview=True)
        if platform != "youtube":
            params.update(platform=platform, text="")
        self.jobs.enqueue(
            kind="resolve" if platform == "youtube" else "source_resolve",
            label=f"Import {len(urls)} {platform_config(platform).label} link(s)", **params,
        )
        self._import_feedback("music", f"Reading {len(urls)} link(s)… Downloads have not started.")
        self._refresh_actions()

    def _finish_import(self, job: Job) -> None:
        if not job.params.get("import_preview") or not job.is_terminal:
            return
        context = job.params.get("results_context", "download")
        if job.state == DONE and isinstance(job.result, list) and job.result:
            panel = self.music_source_tabs if context == "music" else self.source_tabs
            panel.set("Search YouTube")
            self.settings.set("music_source_tab" if context == "music" else "source_tab", "search")
            detail = "Choose Find matches, then review before downloading." if job.kind == "source_resolve" else "Review the list, then choose Download."
            self._import_feedback(context, f"Imported {len(job.result)} items. {detail}")
        elif job.state == DONE:
            self._import_feedback(context, "No items found. Check that the links are accessible and try again.", error=True)
        elif job.state == FAILED:
            self._import_feedback(context, f"Couldn't import links: {_truncate(job.error or 'Please try again.', 120)}", error=True)
        elif job.state == CANCELLED:
            self._import_feedback(context, "Import cancelled. Your links are still here.")

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
        verbose = bool(self.settings.get("verbose"))
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
                verbose=verbose,
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
        self._maybe_enqueue_music_download(
            result.url, label, out_dir=out_dir, cookies=cookies, result=result,
            user_picked=True,
        )

    def _music_download_collection(self, result: SearchResult, *, override: bool) -> None:
        """Download an album/playlist by expanding it into per-track URLs."""
        if result.kind not in ("album", "playlist"):
            self._music_download_one(result, override=override)
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
        verbose = bool(self.settings.get("verbose"))
        collection_url = result.url.strip()
        kind_label = "Album" if result.kind == "album" else "Playlist"
        title = result.display_title(50)
        if collection_url in self._pending_collection_urls:
            self._set_status(f"Still expanding {kind_label.lower()}: {title}…")
            self._music_set_collection_row_busy(collection_url, True)
            return
        self._pending_collection_urls.add(collection_url)
        self._music_set_collection_row_busy(collection_url, True)
        self._set_status(f"Expanding {kind_label.lower()}: {title}…")
        self.music_results_header_label.configure(
            text=f"Results — expanding {kind_label.lower()}: {_truncate(title, 50)}",
        )
        label = f"{kind_label}: expand {result.display_title(60)}"
        self.jobs.enqueue(
            kind="resolve",
            label=label,
            urls=[collection_url],
            cookies_path=cookies,
            verbose=verbose,
            results_context=f"music_{result.kind}_expand",
            out_dir=out_dir,
            collection_title=result.title,
            collection_artist=result.uploader,
            collection_thumbnail_url=result.thumbnail_url,
            collection_kind=result.kind,
            collection_url=collection_url,
        )

    def _music_set_collection_row_busy(self, collection_url: str, busy: bool) -> None:
        """Disable/relabel album/playlist Download buttons while expanding."""
        for row in self._music_result_rows:
            if getattr(row, "result", None) is None:
                continue
            if (row.result.url or "").strip() != collection_url:
                continue
            row.set_collection_busy(busy)

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
        verbose = bool(self.settings.get("verbose"))
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
                    verbose=verbose,
                )

    def _discovery_download(self, track, playlist=None):
        """Queue discovery independently of the Music page's current results."""
        if playlist is None and self.tabs.get() == "Playlists":
            playlist = self.playlists_page.playlist_destination.selected()
        key = track.apple_id or track.key or (track.artist, track.title)
        if playlist is not None:
            key = (key, playlist.id)
        if not hasattr(self, '_discovery_jobs'):
            self._discovery_jobs = {}
        existing = self._discovery_jobs.get(key)
        if existing is not None and existing.state not in (FAILED, CANCELLED):
            return existing
        out_dir = self.settings.get('music_dir')
        if not out_dir:
            out_dir = _pick_folder()
            if not out_dir:
                return None
            self.settings.set('music_dir', out_dir)
        song = MusicTrack(
            artist=track.artist, title=track.title, album=track.album,
            duration_s=round(track.duration_s) if track.duration_s else None,
            cover_url=track.artwork_url or None, track_number=track.track_number,
            disc_number=track.disc_number, source='shazam', source_url=track.apple_url or track.url)
        job = self._enqueue_music_download(
            '', f'Music: {song.display_title(60)}', out_dir=out_dir,
            cookies=self.settings.get('cookies_path') or None, track=song,
            playlist_id=playlist.id if playlist else '')
        self._discovery_jobs[key] = job
        self._set_status(f'Queued: {song.display_title(60)}. Follow progress in Active downloads.')
        return job

    def _music_download_one_track(self, track: MusicTrack, *, override: bool) -> None:
        if not track.is_downloadable() or not track.youtube_url:
            self._set_status("Match this track on YouTube first.")
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
        label = f"Music: {_truncate(track.display_title(60), 60)}"
        self._maybe_enqueue_music_download(
            track.youtube_url, label,
            out_dir=out_dir, cookies=cookies, track=track,
        )

    def _music_match_all(self) -> None:
        if not self.music_tracks:
            self._set_status("No tracks to match.")
            return
        pending = [t for t in self.music_tracks if t.match_status == MATCH_PENDING]
        if not pending:
            self._set_status("All tracks are already matched.")
            return
        cookies = self.settings.get("cookies_path") or None
        self._music_auto_download = False
        job = self.jobs.enqueue(
            kind="source_match_all",
            label=f"Match {len(pending)} track(s) on YouTube",
            tracks=[t.to_dict() for t in self.music_tracks],
            cookies_path=cookies,
            results_context="music",
            **self._music_search_job_params(),
        )
        self._music_match_job_id = job.id

    def _music_retry_track(self, track_index: int) -> None:
        if track_index < 0 or track_index >= len(self.music_tracks):
            return
        from dataclasses import replace

        self.music_tracks[track_index] = replace(
            self.music_tracks[track_index],
            match_status=MATCH_PENDING,
        )
        self._music_render_results()
        self._music_match_all()

    def _music_retry_all_failed(self) -> None:
        from dataclasses import replace

        changed = False
        for i, track in enumerate(self.music_tracks):
            if track.match_status == MATCH_FAILED:
                self.music_tracks[i] = replace(track, match_status=MATCH_PENDING)
                changed = True
        if not changed:
            self._set_status("No failed tracks to retry.")
            return
        self._music_render_results()
        self._music_match_all()

    def _music_show_match(self, track: MusicTrack, track_index: int) -> None:
        if not track.youtube_url:
            self._set_status("No YouTube match for this track.")
            return
        _MatchDetailDialog(self, track, track_index)

    def _music_review_matches(self) -> None:
        matched = [t for t in self.music_tracks if t.youtube_url]
        if not matched:
            self._set_status("No matched tracks to review.")
            return
        _MatchReviewDialog(self, self.music_tracks)

    def _music_enqueue_matched_downloads(self, out_dir: str) -> None:
        cookies = self.settings.get("cookies_path") or None
        items: list[tuple[str, str, SearchResult | None, MusicTrack | None]] = []
        for track in self.music_tracks:
            if not track.is_downloadable() or not track.youtube_url:
                continue
            label = f"Music: {_truncate(track.display_title(60), 60)}"
            items.append((track.youtube_url, label, None, track))
        self._enqueue_music_downloads_batch(out_dir, cookies, items)

    def _music_download_all(self, *, override: bool) -> None:
        if self._music_showing_tracks:
            downloadable = [t for t in self.music_tracks if t.is_downloadable()]
            pending = [t for t in self.music_tracks if t.match_status == MATCH_PENDING]
            if pending and not downloadable:
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
                self._music_pending_out_dir = out_dir
                cookies = self.settings.get("cookies_path") or None
                job = self.jobs.enqueue(
                    kind="source_match_all",
                    label=f"Match {len(pending)} track(s) on YouTube",
                    tracks=[t.to_dict() for t in self.music_tracks],
                    cookies_path=cookies,
                    results_context="music",
                    auto_download=True,
                    **self._music_search_job_params(),
                )
                self._music_match_job_id = job.id
                return
            if not downloadable:
                self._set_status("No matched tracks to download.")
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
            self._music_enqueue_matched_downloads(out_dir)
            return

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
        items = [
            (
                result.url,
                f"Music: {_truncate(result.display_title(60), 60)}",
                result,
                None,
            )
            for result in self.music_results
            if result.kind == "track"
        ]
        if not items:
            self._set_status("No tracks to download (albums/playlists need Download album).")
            return
        self._enqueue_music_downloads_batch(out_dir, cookies, items)

    # ====================== Rendering ======================================

    def _make_empty_state(self, parent, title, detail):
        frame = ctk.CTkFrame(parent, fg_color=SURFACE)
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(0, 8))
        ctk.CTkLabel(frame, text=detail, text_color=MUTED,
                     font=ctk.CTkFont(size=14), justify="center").pack()
        return frame

    def _update_empty_states(self) -> None:
        self._refresh_actions()
        for name, has_content in (("_video_empty", bool(self.results)),
                                  ("_music_empty", bool(self.music_results or self.music_tracks))):
            frame = getattr(self, name, None)
            if frame is not None:
                shown = bool(frame.place_info())
                if has_content and shown:
                    frame.place_forget()
                elif not has_content and not shown:
                    frame.place(relx=0.5, rely=0.4, anchor="center")
                    frame.lift()

    def _render_results(self) -> None:
        self._update_empty_states()
        self._clear_loading_indicator()
        self._results_render_token += 1
        token = self._results_render_token
        for row in self._result_rows:
            row.destroy()
        self._result_rows.clear()

        if self.results:
            self.results_header_label.configure(text=f"Results ({len(self.results)})")
        else:
            self.results_header_label.configure(
                text="Results"
            )
        self._update_search_scroll_footer()
        self._schedule_scroll_bottom_check()
        pending = list(self.results)
        self._render_results_chunk(token, pending, 0)

    def _render_results_chunk(
        self,
        token: int,
        pending: list[SearchResult],
        start: int,
    ) -> None:
        if token != self._results_render_token:
            return
        end = min(start + self._RESULT_RENDER_CHUNK, len(pending))
        for r in pending[start:end]:
            self._result_rows.append(_ResultRow(self.results_frame, r, self))
        if end < len(pending):
            self.after(
                1,
                lambda: self._render_results_chunk(token, pending, end),
            )
        else:
            self._schedule_results_scroll_height_sync()

    def _clear_results(self) -> None:
        self.results = []
        self._search_query = None
        self._search_loading_more = False
        self._search_more_exhausted = False
        self._clear_loading_indicator()
        self._render_results()

    def _music_render_results(self) -> None:
        self._update_empty_states()
        if (
            self._music_alternate_open_index is not None
            and self._music_rematch_panel is not None
        ):
            self._music_update_input_stage()
            return
        self._music_clear_loading_indicator()
        self._music_alternate_panels.clear()
        self._music_render_token += 1
        token = self._music_render_token
        for row in self._music_result_rows:
            row.destroy()
        self._music_result_rows.clear()
        for row in self._music_track_rows:
            row.destroy()
        self._music_track_rows.clear()

        if self._music_showing_tracks:
            matched = sum(1 for t in self.music_tracks if t.is_downloadable())
            failed = sum(
                1 for t in self.music_tracks if t.match_status == MATCH_FAILED
            )
            pending_n = sum(
                1 for t in self.music_tracks if t.match_status == MATCH_PENDING
            )
            if self.music_tracks:
                header = f"Tracks ({len(self.music_tracks)})"
                if pending_n:
                    header += f" — {pending_n} need YouTube match"
                elif failed:
                    header += f" — {matched} matched, {failed} failed"
                else:
                    header += f" — {matched} ready"
                self.music_results_header_label.configure(text=header)
            else:
                self.music_results_header_label.configure(
                    text="Songs & albums",
                )
            self._music_header_actions = {
                "match": bool(pending_n),
                "retry": bool(failed),
                "retry_label": (
                    f"Retry {failed} failed" if failed else "Retry failed"
                ),
                "review": bool(matched),
            }
            pending_tracks = list(enumerate(self.music_tracks))
            self._music_render_tracks_chunk(token, pending_tracks, 0)
            self._music_update_input_stage()
            return

        self._music_header_actions = {}
        if self.music_results:
            self.music_results_header_label.configure(
                text=f"Results ({len(self.music_results)})",
            )
        else:
            self.music_results_header_label.configure(
                text="Songs & albums",
            )
        self._music_update_scroll_footer()
        self._schedule_scroll_bottom_check()
        pending = list(enumerate(self.music_results))
        self._music_render_search_chunk(token, pending, 0)
        self._music_update_input_stage()

    def _music_render_tracks_chunk(
        self,
        token: int,
        pending: list[tuple[int, MusicTrack]],
        start: int,
    ) -> None:
        if token != self._music_render_token:
            return
        end = min(start + self._RESULT_RENDER_CHUNK, len(pending))
        for i, track in pending[start:end]:
            self._music_track_rows.append(
                _MusicTrackRow(
                    self.music_results_frame, track, self,
                    track_index=i,
                ),
            )
        if end < len(pending):
            self.after(
                1,
                lambda: self._music_render_tracks_chunk(token, pending, end),
            )
        else:
            self._schedule_results_scroll_height_sync()

    def _music_render_search_chunk(
        self,
        token: int,
        pending: list[tuple[int, SearchResult]],
        start: int,
    ) -> None:
        if token != self._music_render_token:
            return
        end = min(start + self._RESULT_RENDER_CHUNK, len(pending))
        for i, r in pending[start:end]:
            self._music_result_rows.append(
                _ResultRow(
                    self.music_results_frame, r, self, mode="music",
                    result_index=i,
                ),
            )
        if end < len(pending):
            self.after(
                1,
                lambda: self._music_render_search_chunk(token, pending, end),
            )
        else:
            self._schedule_results_scroll_height_sync()

    def _music_track_tally(self) -> int:
        """Song rows in the current music search (excludes album/playlist rows)."""
        return sum(
            1 for r in self.music_results
            if r.kind not in ("album", "playlist")
        )

    def _music_update_scroll_footer(self) -> None:
        if self._music_scroll_footer is not None:
            try:
                self._music_scroll_footer.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._music_scroll_footer = None
        track_count = self._music_track_tally()
        if track_count > self._music_track_count:
            self._music_track_count = track_count
        if (
            self._music_search_query
            and not self._music_search_more_exhausted
            and not self._music_showing_tracks
            and self._music_track_count >= self._music_search_page_size
        ):
            self._music_scroll_footer = ctk.CTkButton(
                self.music_results_footer,
                text="Load more results",
                command=self._music_load_more_results,
                fg_color="transparent",
                border_width=1,
                text_color=TEXT,
            )
            self._music_scroll_footer.pack(fill="x", padx=8, pady=4)

    def _update_search_scroll_footer(self) -> None:
        if self._search_scroll_footer is not None:
            try:
                self._search_scroll_footer.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._search_scroll_footer = None
        if (
            self._search_query
            and not self._search_more_exhausted
            and len(self.results) >= self._search_page_size
        ):
            self._search_scroll_footer = ctk.CTkButton(
                self.results_footer,
                text="Load more results",
                command=self._load_more_results,
                fg_color="transparent",
                border_width=1,
                text_color=TEXT,
            )
            self._search_scroll_footer.pack(fill="x", padx=8, pady=4)

    def _music_has_workspace_content(self) -> bool:
        return bool(self.music_tracks) or bool(self.music_results)

    def _music_update_input_stage(self) -> None:
        """Keep search accessible while presenting review/matching context."""
        has_content = self._music_has_workspace_content()
        rematch_open = self._music_alternate_open_index is not None
        show_compose = not has_content and not rematch_open

        self._music_context_strip.pack_forget()
        self._music_context_match_btn.pack_forget()
        self._music_context_search_btn.pack_forget()
        self._music_context_new_btn.pack_forget()

        if show_compose:
            self._music_input_collapsed = False
            self.music_source_tabs.pack(fill="x")
            return

        self._music_input_collapsed = False
        self.music_source_tabs.pack(fill="x")

        # Context strip summary.
        if rematch_open:
            label = "Rematch — pick an alternate YouTube match"
            self._music_context_label.configure(text=label)
            self._music_context_new_btn.configure(text="Back to list")
            self._music_context_new_btn.configure(command=self._music_close_rematch)
            self._music_context_new_btn.pack(side="right", padx=2)
        elif self._music_showing_tracks and self.music_tracks:
            platform = self._music_platform_id()
            plat_label = platform_config(platform).label
            pending_n = sum(
                1 for t in self.music_tracks if t.match_status == MATCH_PENDING
            )
            failed = sum(
                1 for t in self.music_tracks if t.match_status == MATCH_FAILED
            )
            bits = [f"{plat_label} · {len(self.music_tracks)} tracks"]
            if pending_n:
                bits.append(f"{pending_n} need match")
            elif failed:
                bits.append(f"{failed} failed")
            else:
                bits.append("ready")
            self._music_context_label.configure(text=" · ".join(bits))
            self._music_context_new_btn.configure(text="Import links…")
            self._music_context_new_btn.configure(command=self._music_new_link)
            self._music_context_new_btn.pack(side="right", padx=2)
            if pending_n:
                self._music_context_label.configure(text="Find YouTube matches, then review before downloading.")
        else:
            return  # Search field and result header already provide this context.

        self._music_context_strip.pack(fill="x", pady=(2, 0))

    def _music_new_search(self) -> None:
        """Focus the next query without clearing the current results."""
        if self._music_alternate_open_index is not None:
            self._music_close_rematch()
        self.music_source_tabs.set("Search YouTube")
        self.settings.set("music_source_tab", "search")
        self._focus_search()

    def _music_new_link(self) -> None:
        """Open import while retaining current results until the new import succeeds."""
        if self._music_alternate_open_index is not None:
            self._music_close_rematch()
        self.music_source_tabs.set("Paste Link")
        self.settings.set("music_source_tab", "paste")
        self.music_paste_box.focus_set()

    def _music_close_rematch(self, *, render: bool = True) -> None:
        self._music_alternate_open_index = None
        panel = self._music_rematch_panel
        self._music_rematch_panel = None
        if panel is not None:
            panel.destroy()
        self._music_alternate_panels.clear()
        if render:
            # Restore scrollable list in results body.
            if not self.music_results_frame.winfo_ismapped():
                self.music_results_frame.pack(fill="both", expand=True)
            self._music_render_results()
            self._music_update_input_stage()
            self._schedule_results_scroll_height_sync()

    def _music_open_rematch(self, index: int, *, mode: str = "track") -> None:
        """Open full-height rematch workspace replacing the track list."""
        self._music_alternate_open_index = index
        self._music_alternate_mode = mode
        # Tear down list rows so rematch owns the viewport.
        self._music_render_token += 1
        for row in self._music_result_rows:
            row.destroy()
        self._music_result_rows.clear()
        for row in self._music_track_rows:
            row.destroy()
        self._music_track_rows.clear()
        if self._music_rematch_panel is not None:
            self._music_rematch_panel.destroy()
            self._music_rematch_panel = None
        self.music_results_frame.pack_forget()
        self.music_results_footer.grid_remove()

        track = None
        current = None
        if mode == "track" and 0 <= index < len(self.music_tracks):
            track = self.music_tracks[index]
            self.music_results_header_label.configure(
                text=f"Rematch — {track.display_title(60)}",
            )
        elif mode == "search" and 0 <= index < len(self.music_results):
            current = self.music_results[index]
            self.music_results_header_label.configure(
                text=f"Change — {current.display_title(60)}",
            )
        else:
            self._music_alternate_open_index = None
            self.music_results_frame.pack(fill="both", expand=True)
            self.music_results_footer.grid()
            self._music_render_results()
            return

        self._music_rematch_panel = _MusicAlternatePanel(
            self._music_results_body,
            index,
            self,
            track=track,
            current=current,
            fill_parent=True,
        )
        self._music_update_input_stage()
        self._schedule_results_scroll_height_sync()

    def _music_toggle_alternate(self, track_index: int) -> None:
        if self._music_alternate_open_index == track_index:
            self._music_close_rematch()
        else:
            mode = "track" if self._music_showing_tracks else "search"
            self._music_open_rematch(track_index, mode=mode)

    def _music_apply_manual_match(
        self, track_index: int, result: SearchResult,
    ) -> None:
        if track_index < 0 or track_index >= len(self.music_tracks):
            return
        self.music_tracks[track_index] = self.music_tracks[track_index].with_match(
            result,
        )
        self._music_close_rematch(render=False)
        self.music_results_frame.pack(fill="both", expand=True)
        self.music_results_footer.grid()
        self._music_render_results()
        self._music_update_input_stage()
        self._set_status(f"Matched to: {result.display_title(60)}")

    def _music_apply_search_alternate(
        self, result_index: int, result: SearchResult,
    ) -> None:
        if result_index < 0 or result_index >= len(self.music_results):
            return
        self.music_results[result_index] = result
        self._music_close_rematch(render=False)
        self.music_results_frame.pack(fill="both", expand=True)
        self.music_results_footer.grid()
        self._music_render_results()
        self._music_update_input_stage()
        self._set_status(f"Changed to: {result.display_title(60)}")

    def _music_search_alternate(
        self, index: int, query: str, *, mode: str = "track",
    ) -> None:
        query = (query or "").strip()
        if not query:
            self._set_status("Enter a search query.")
            return
        cookies = self.settings.get("cookies_path") or None
        ctx = "music_rematch" if mode == "track" else "music_search_rematch"
        self.jobs.enqueue(
            kind="search",
            label=f"Alternate: {_truncate(query, 40)}",
            query=query,
            limit=15,
            cookies_path=cookies,
            videos_only=True,
            results_context=ctx,
            track_index=index,
            **self._music_search_job_params(),
        )

    def _music_clear_results(self, *, confirm: bool = True) -> None:
        if (
            confirm
            and self._music_has_workspace_content()
            and self._active_rows
        ):
            if not messagebox.askyesno(
                "Clear results",
                "Clear the current track list?\n\n"
                "Active downloads will keep running.",
            ):
                return
        self._music_close_rematch(render=False)
        self.music_results_frame.pack(fill="both", expand=True)
        try:
            self.music_results_footer.grid()
        except Exception:  # noqa: BLE001
            pass
        self.music_results = []
        self.music_tracks = []
        self._music_track_count = 0
        self._music_album_count = 0
        self._music_albums_exhausted = False
        self._music_showing_tracks = False
        self._music_auto_download = False
        self._music_pending_out_dir = None
        self._music_search_query = None
        self._music_search_loading_more = False
        self._music_search_more_exhausted = False
        self._music_clear_loading_indicator()
        self._music_render_results()
        self._music_update_input_stage()

    # ====================== Infinite scroll =================================

    def _bind_results_pagination_watch(
        self, scroll_frame: ctk.CTkScrollableFrame,
    ) -> None:
        """Re-check pagination when result rows change the scrollable height."""
        pending: list[str | None] = [None]

        def _on_configure(_event=None) -> None:
            job = pending[0]
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
            pending[0] = self.after(60, self._check_scroll_bottom)

        try:
            scroll_frame.bind("<Configure>", _on_configure, add="+")
            scroll_frame._parent_canvas.bind("<Configure>", _on_configure, add="+")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass

    def _schedule_scroll_bottom_check(self) -> None:
        """Re-check after layout settles (rows/footer may not have size yet)."""
        self.after_idle(self._check_scroll_bottom)
        self.after(120, self._check_scroll_bottom)
        self.after(350, self._check_scroll_bottom)

    def _scroll_frame_near_bottom(
        self,
        scroll_frame: ctk.CTkScrollableFrame,
        *,
        loaded_count: int,
        page_size: int,
    ) -> bool:
        """True when the list overflows and the user has scrolled near the end.

        Do not treat "all items fit in the viewport" as near-bottom — that used
        to auto-fetch another page right after a Limit:10 search (10 → 20)
        because short results never need scrolling. Use the Load more button
        or scroll when content is taller than the pane.
        """
        if loaded_count < page_size:
            return False
        try:
            canvas = scroll_frame._parent_canvas  # noqa: SLF001
            scroll_frame.update_idletasks()
            canvas.update_idletasks()
            top, bottom = canvas.yview()
            view_h = canvas.winfo_height()
            if view_h <= 1:
                return False
            bbox = canvas.bbox("all")
            if not bbox:
                return False
            content_h = bbox[3] - bbox[1]
            # Content fits without scrolling — honor the search Limit as-is.
            # (yview is often (0, 1) here, which would false-trigger below.)
            if content_h <= view_h + 8:
                return False
            if bottom >= 0.88:
                return True
            # yview fractions can stall slightly below 1.0 on macOS/CTk.
            visible_bottom = (top * content_h) + view_h
            return visible_bottom >= content_h - 24
        except (AttributeError, ValueError, Exception):  # noqa: BLE001
            return False

    def _check_scroll_bottom(self) -> None:
        """Detect when the user has scrolled near the bottom of results
        and kick off a `search_more` job to append the next page."""
        try:
            if self.tabs.get() == "Video":
                if (
                    self._search_query
                    and not self._search_loading_more
                    and not self._search_more_exhausted
                    and len(self.results) >= self._search_page_size
                ):
                    if self._scroll_frame_near_bottom(
                        self.results_frame,
                        loaded_count=len(self.results),
                        page_size=self._search_page_size,
                    ):
                        self._load_more_results()
            elif self.tabs.get() == "Music":
                if (
                    not self._music_showing_tracks
                    and self._music_search_query
                    and not self._music_search_loading_more
                    and not self._music_search_more_exhausted
                    and self._music_track_count >= self._music_search_page_size
                ):
                    if self._scroll_frame_near_bottom(
                        self.music_results_frame,
                        loaded_count=self._music_track_count,
                        page_size=self._music_search_page_size,
                    ):
                        self._music_load_more_results()
        except (AttributeError, ValueError):
            pass

    def _poll_scroll_bottom(self) -> None:
        self._check_scroll_bottom()
        self.after(400, self._poll_scroll_bottom)

    def _load_more_results(self) -> None:
        if not self._search_query:
            self._set_status("Load more is only available for text searches.")
            return
        if self._search_loading_more:
            return
        if self._search_more_exhausted:
            self._set_status("No more results.")
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
        if not self._music_search_query:
            self._set_status("Load more is only available for text searches.")
            return
        if self._music_search_loading_more:
            return
        if self._music_search_more_exhausted:
            self._set_status("No more results.")
            return
        self._music_search_loading_more = True
        already_loaded = self._music_track_count
        new_limit = already_loaded + self._music_search_page_size
        cookies = self.settings.get("cookies_path") or None
        album_fetch = 0
        if self._music_search_include_albums and not self._music_albums_exhausted:
            album_fetch = 2
        self._music_show_loading_indicator(
            f"Loading more results ({already_loaded} → {new_limit})...",
        )
        self.jobs.enqueue(
            kind="search_more",
            label=f"Music load more: {self._music_search_query[:50]}",
            query=self._music_search_query,
            limit=new_limit,
            already_loaded=already_loaded,
            album_offset=self._music_album_count,
            album_fetch_count=album_fetch,
            cookies_path=cookies,
            videos_only=True,
            results_context="music",
            verbose=bool(self.settings.get("verbose")),
            use_youtube_music=self._music_use_youtube_music,
            audio_only=self._music_search_audio_only,
            album_limit=max(
                1, int(self.settings.get("music_album_search_limit") or 5),
            ),
            include_albums=bool(album_fetch),
            include_playlists=False,
        )

    def _show_loading_indicator(self, text: str) -> None:
        self._clear_loading_indicator()
        self._loading_more_label = ctk.CTkLabel(
            self.results_footer, text=text,
            text_color=MUTED,
        )
        self._loading_more_label.pack(fill="x", padx=8, pady=4)

    def _clear_loading_indicator(self) -> None:
        if self._loading_more_label is not None:
            try:
                self._loading_more_label.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._loading_more_label = None

    def _music_show_loading_indicator(self, text: str) -> None:
        self._music_clear_loading_indicator()
        if self._music_scroll_footer is not None:
            try:
                self._music_scroll_footer.pack_forget()
            except Exception:  # noqa: BLE001
                pass
        self._music_loading_more_label = ctk.CTkLabel(
            self.music_results_footer, text=text,
            text_color=MUTED,
        )
        self._music_loading_more_label.pack(fill="x", padx=8, pady=4)

    def _music_clear_loading_indicator(self) -> None:
        if self._music_loading_more_label is not None:
            try:
                self._music_loading_more_label.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._music_loading_more_label = None
        self._music_update_scroll_footer()

    def _append_more_results(self, new_items: list[SearchResult]) -> None:
        for r in new_items:
            self.results.append(r)
            self._result_rows.append(_ResultRow(self.results_frame, r, self))
        self.results_header_label.configure(text=f"Results ({len(self.results)})")
        self._update_search_scroll_footer()
        self._schedule_scroll_bottom_check()
        self._schedule_results_scroll_height_sync()

    def _music_append_more_results(
        self,
        new_tracks: list[SearchResult],
        new_albums: list[SearchResult] | None = None,
    ) -> None:
        self._music_showing_tracks = False
        self.music_tracks = []
        if new_albums:
            self._music_insert_album_results(new_albums)
        for r in new_tracks:
            self.music_results.append(r)
            self._music_result_rows.append(
                _ResultRow(
                    self.music_results_frame, r, self, mode="music",
                    result_index=len(self.music_results) - 1,
                ),
            )
        self.music_results_header_label.configure(
            text=f"Results ({len(self.music_results)})",
        )
        self._music_update_scroll_footer()
        self._schedule_scroll_bottom_check()
        self._schedule_results_scroll_height_sync()

    def _music_insert_album_results(self, new_albums: list[SearchResult]) -> None:
        if not new_albums:
            return
        last_album_idx = -1
        for i, r in enumerate(self.music_results):
            if r.kind in ("album", "playlist"):
                last_album_idx = i
        insert_at = last_album_idx + 1 if last_album_idx >= 0 else min(3, len(self.music_results))
        for offset, album in enumerate(new_albums):
            self.music_results.insert(insert_at + offset, album)
        self._music_rebuild_result_rows_from(insert_at)

    def _music_rebuild_result_rows_from(self, start_index: int) -> None:
        self._music_render_token += 1
        token = self._music_render_token
        for row in self._music_result_rows[start_index:]:
            row.destroy()
        self._music_result_rows = self._music_result_rows[:start_index]
        pending = list(enumerate(self.music_results[start_index:], start=start_index))
        self._music_render_search_chunk(token, pending, 0)

    def _music_apply_partial_search(self, job: Job) -> None:
        if job.id != self._music_stream_job_id:
            return
        payload = job.result
        if not isinstance(payload, dict) or not payload.get("partial"):
            return
        phase = str(payload.get("phase") or "")
        items: list[SearchResult] = list(payload.get("items") or [])
        if phase == "tracks":
            self.music_results = list(items)
            self._music_showing_tracks = False
            self._music_track_count = len(items)
            self.music_tracks = []
            self._music_render_results()
            self.music_results_header_label.configure(
                text=f"Results ({len(self.music_results)}) — loading albums…",
            )
        elif phase == "collections" and items:
            # Don't rebuild rows mid-stream — inserting albums destroys and
            # recreates the track list and freezes the UI. Albums land on the
            # final search result (or a lightweight merge below).
            self._music_pending_stream_albums = items
            self._music_album_count = len(items)
            self.music_results_header_label.configure(
                text=f"Results ({len(self.music_results)}) — loading details…",
            )
        elif phase == "enrich_tracks" and items:
            self._music_update_enriched_tracks(items)
            self.music_results_header_label.configure(
                text=f"Results ({len(self.music_results)}) — finishing…",
            )
        self._schedule_results_scroll_height_sync()

    def _music_update_collections(self, collections: list[SearchResult]) -> None:
        by_url = {(r.kind, r.url): r for r in collections if r.url}
        for i, r in enumerate(self.music_results):
            if r.kind not in ("album", "playlist"):
                continue
            key = (r.kind, r.url)
            new_r = by_url.get(key)
            if new_r is None:
                continue
            self.music_results[i] = new_r
            if i < len(self._music_result_rows):
                self._music_result_rows[i].update_result(new_r)

    def _music_update_enriched_tracks(self, enriched: list[SearchResult]) -> None:
        track_idx = 0
        for i, r in enumerate(self.music_results):
            if r.kind in ("album", "playlist"):
                continue
            if track_idx >= len(enriched):
                break
            new_r = enriched[track_idx]
            track_idx += 1
            self.music_results[i] = new_r
            if i < len(self._music_result_rows):
                self._music_result_rows[i].update_result(new_r)

    def _music_apply_partial_match(self, job: Job) -> None:
        if job.id != self._music_match_job_id:
            return
        from .sources.base import MusicTrack

        tracks = [
            t if isinstance(t, MusicTrack) else MusicTrack.from_dict(t)
            for t in job.result
        ]
        self._music_showing_tracks = True
        self.music_results = []
        if not self._music_track_rows:
            self.music_tracks = tracks
            self._music_render_results()
            return
        old = self.music_tracks
        self.music_tracks = tracks
        for i, (new_t, old_t) in enumerate(zip(tracks, old)):
            if (
                new_t.match_status != old_t.match_status
                or new_t.youtube_url != old_t.youtube_url
            ):
                self._music_refresh_track_row(i, new_t)
        self._music_update_match_header()

    def _music_refresh_track_row(self, index: int, track: MusicTrack) -> None:
        if index < 0 or index >= len(self._music_track_rows):
            return
        if self._music_alternate_open_index is not None:
            # Rematch workspace owns the body; skip row rebuild.
            self._music_update_match_header()
            return
        self._music_track_rows[index].destroy()
        self._music_track_rows[index] = _MusicTrackRow(
            self.music_results_frame, track, self,
            track_index=index,
        )

    def _music_update_match_header(self) -> None:
        if not self.music_tracks:
            return
        matched = sum(1 for t in self.music_tracks if t.is_downloadable())
        failed = sum(1 for t in self.music_tracks if t.match_status == MATCH_FAILED)
        pending = sum(1 for t in self.music_tracks if t.match_status == MATCH_PENDING)
        header = f"Tracks ({len(self.music_tracks)})"
        if pending:
            header += f" — matching: {matched} done, {pending} left"
        elif failed:
            header += f" — {matched} matched, {failed} failed"
        else:
            header += f" — {matched} ready"
        self.music_results_header_label.configure(text=header)
        self._music_update_input_stage()

    def _add_recent_job(self, job: Job) -> None:
        key = _recent_dedupe_key(job)
        row = self._recent_rows.get(key)
        if row is not None:
            row.merge(job)
        else:
            row = _RecentRow(self.recent_frame, job, self)
            self._recent_rows[key] = row
        ids_sorted = sorted(
            self._recent_rows.keys(),
            key=lambda k: self._recent_rows[k].latest_job_id,
            reverse=True,
        )
        for excess_key in ids_sorted[10:]:
            rr = self._recent_rows.pop(excess_key, None)
            if rr is not None:
                rr.frame.destroy()
        self._reorder_recent_rows()

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
            if (
                job.kind == "search"
                and ctx == "music"
                and isinstance(job.result, dict)
                and job.result.get("partial")
            ):
                self._music_apply_partial_search(job)
            elif (
                job.kind == "source_match_all"
                and ctx == "music"
                and isinstance(job.result, list)
            ):
                self._music_apply_partial_match(job)
            if job.kind == "search" and ctx in ("music_rematch", "music_search_rematch"):
                panel = self._music_alternate_panels.get(
                    job.params.get("track_index"),
                )
                if panel is None:
                    panel = self._music_rematch_panel
                if panel is not None:
                    panel.set_searching(job.progress_msg or "Searching…")
            elif job.kind == "search":
                hdr = (
                    self.music_results_header_label
                    if ctx == "music"
                    else self.results_header_label
                )
                hdr.configure(
                    text=f"Results — searching: {_truncate(job.progress_msg or '...', 60)}"
                )
            elif job.kind in ("resolve", "source_resolve", "source_match_all"):
                ctx = job.params.get("results_context", "download")
                music_ctx = (
                    ctx == "music"
                    or ctx in ("music_album_expand", "music_playlist_expand")
                )
                hdr = (
                    self.music_results_header_label
                    if music_ctx
                    else self.results_header_label
                )
                if ctx in ("music_album_expand", "music_playlist_expand"):
                    kind = "album" if "album" in ctx else "playlist"
                    verb = f"expanding {kind}"
                    self._set_status(
                        f"Expanding {kind}: {_truncate(job.progress_msg or '...', 80)}",
                    )
                else:
                    verb = {
                        "resolve": "resolving",
                        "source_resolve": "resolving",
                        "source_match_all": "matching",
                    }.get(job.kind, "working")
                hdr.configure(
                    text=f"Results — {verb}: {_truncate(job.progress_msg or '...', 60)}"
                )
        else:
            if job.kind == "music":
                self._maybe_restore_playlist_parallel()
            # Job is terminal — remove from active, add to recent (unless it was a search/resolve)
            row = self._active_rows.pop(job.id, None)
            if row is not None:
                row.frame.destroy()

            # Search / resolve completion: replace results list.
            if job.kind in ("search", "resolve"):
                ctx = job.params.get("results_context", "download")
                if job.kind == "search" and ctx in ("music_rematch", "music_search_rematch"):
                    panel = self._music_alternate_panels.get(
                        job.params.get("track_index"),
                    )
                    if panel is None:
                        panel = self._music_rematch_panel
                    if job.state == DONE and panel is not None:
                        results = job.result if isinstance(job.result, list) else []
                        panel.set_results(results)
                        self._set_status(
                            f"Found {len(results)} alternate YouTube result(s).",
                        )
                    elif job.state == FAILED and panel is not None:
                        panel.set_error(job.error or "Search failed")
                        self._set_status(f"Alternate search failed: {job.error}")
                    elif job.state == CANCELLED and panel is not None:
                        panel.set_searching("Cancelled")
                elif job.state == DONE and isinstance(job.result, list) and ctx in ("music_album_expand", "music_playlist_expand"):
                    collection_url = str(job.params.get("collection_url") or "").strip()
                    if collection_url:
                        self._pending_collection_urls.discard(collection_url)
                        self._music_set_collection_row_busy(collection_url, False)
                    if job.id in self._terminal_side_effects_handled:
                        pass
                    else:
                        self._terminal_side_effects_handled.add(job.id)
                        from dataclasses import replace

                        from .metadata.parse import parse_youtube_track, primary_album_artist
                        from .sources.base import MusicTrack

                        out_dir = str(job.params.get("out_dir") or self.settings.get("music_dir") or "")
                        if not out_dir:
                            self._set_status("Missing music output folder.")
                        else:
                            cookies = job.params.get("cookies_path") or None
                            collection_title = str(job.params.get("collection_title") or "").strip()
                            collection_artist = str(
                                job.params.get("collection_artist") or ""
                            ).strip()
                            collection_cover_url = str(
                                job.params.get("collection_thumbnail_url") or ""
                            ).strip() or None
                            tracks = [r for r in job.result if isinstance(r, SearchResult)]
                            items: list[tuple[str, str, SearchResult | None, MusicTrack | None]] = []
                            seen_urls: set[str] = set()
                            for track_num, r in enumerate(tracks, start=1):
                                if not r.url:
                                    continue
                                mt = MusicTrack.from_search_result(r)
                                parsed = parse_youtube_track(r.title, r.uploader)
                                # Keep full track credits on Artist; Album Artist
                                # is primary-only for Apple Music grouping.
                                track_artist = (
                                    parsed.artist
                                    or mt.artist
                                    or collection_artist
                                )
                                title = parsed.title or mt.title or r.title
                                album_artist = primary_album_artist(
                                    track_artist,
                                    album_artist=collection_artist,
                                )
                                if track_artist or title:
                                    mt = replace(
                                        mt,
                                        artist=track_artist,
                                        title=title,
                                    )
                                mt = replace(
                                    mt,
                                    album=normalize_collection_title(collection_title),
                                    album_artist=album_artist,
                                    track_number=track_num,
                                    cover_url=collection_cover_url or mt.cover_url,
                                )
                                label = f"Music: {_truncate(mt.display_title(60), 60)}"
                                url = (mt.youtube_url or r.url).strip()
                                if not url or url in seen_urls:
                                    continue
                                seen_urls.add(url)
                                items.append((url, label, None, mt))
                            if not items:
                                self._set_status("No tracks found to download.")
                            else:
                                # Defer so nested dialogs don't re-enter the
                                # message queue while this handler is active.
                                def _start_batch(
                                    od=out_dir,
                                    ck=cookies,
                                    batch_items=items,
                                    title=collection_title,
                                ) -> None:
                                    self._enqueue_music_downloads_batch(od, ck, batch_items)
                                    self._set_status(
                                        f"Queued {len(batch_items)} track(s) "
                                        f"from {title or 'collection'}.",
                                    )

                                self._set_status(
                                    f"Found {len(items)} track(s) — queuing downloads…",
                                )
                                self.after(0, _start_batch)

                elif job.state in (FAILED, CANCELLED) and ctx in ("music_album_expand", "music_playlist_expand"):
                    collection_url = str(job.params.get("collection_url") or "").strip()
                    if collection_url:
                        self._pending_collection_urls.discard(collection_url)
                        self._music_set_collection_row_busy(collection_url, False)
                    kind = "album" if "album" in ctx else "playlist"
                    if job.state == FAILED:
                        self._set_status(
                            f"Failed to expand {kind}: {job.error or 'unknown error'}",
                        )
                        self.music_results_header_label.configure(
                            text=f"Results — {kind} expand failed",
                        )
                    else:
                        self._set_status(f"{kind.capitalize()} expand cancelled.")

                elif job.state == DONE and isinstance(job.result, list):
                    if ctx == "music":
                        had_stream = job.id == self._music_stream_job_id
                        self._music_stream_job_id = None
                        prior = list(self.music_results)
                        final = list(job.result)
                        prior_tracks = [
                            r for r in prior if r.kind not in ("album", "playlist")
                        ]
                        final_tracks = [
                            r for r in final if r.kind not in ("album", "playlist")
                        ]
                        final_albums = [
                            r for r in final if r.kind in ("album", "playlist")
                        ]
                        self._music_albums_exhausted = False
                        if self._music_use_youtube_music and self._music_search_include_albums:
                            album_limit = max(
                                1,
                                int(self.settings.get("music_album_search_limit") or 5),
                            )
                            if len(final_albums) < album_limit:
                                self._music_albums_exhausted = True
                        self.music_tracks = []
                        self._music_showing_tracks = False
                        self._music_pending_stream_albums = []
                        streamed_tracks_ok = (
                            had_stream
                            and self._music_result_rows
                            and [r.url for r in prior_tracks]
                            == [r.url for r in final_tracks]
                            and len(self._music_result_rows) == len(prior_tracks)
                            and not any(
                                r.kind in ("album", "playlist") for r in prior
                            )
                        )
                        if streamed_tracks_ok:
                            # Keep visible track rows; refresh metadata in place.
                            self.music_results = list(prior_tracks)
                            for i, new_r in enumerate(final_tracks):
                                self.music_results[i] = new_r
                                if i < len(self._music_result_rows):
                                    self._music_result_rows[i].update_result(new_r)
                            if final_albums:
                                self._music_insert_album_results(final_albums)
                            self._music_track_count = self._music_track_tally()
                            self._music_album_count = sum(
                                1 for r in self.music_results
                                if r.kind in ("album", "playlist")
                            )
                            self.music_results_header_label.configure(
                                text=f"Results ({len(self.music_results)})",
                            )
                            self._music_update_scroll_footer()
                            self._music_update_input_stage()
                        else:
                            self.music_results = final
                            self._music_track_count = self._music_track_tally()
                            self._music_album_count = sum(
                                1 for r in self.music_results
                                if r.kind in ("album", "playlist")
                            )
                            if had_stream and prior == final and self._music_result_rows:
                                self.music_results_header_label.configure(
                                    text=f"Results ({len(final)})",
                                )
                                self._music_update_input_stage()
                            else:
                                self._music_render_results()
                    else:
                        self.results = list(job.result)
                        self._render_results()
                    self._set_status(
                        f"{job.kind.capitalize()} returned {len(job.result)} result(s)."
                    )
                    if job.kind == "search":
                        if ctx == "music":
                            pending = self._music_pending_search_query
                            raw_query = str(job.params.get("query") or "").strip()
                            self._music_search_query = pending or (
                                raw_query if raw_query and not is_url(raw_query) else None
                            )
                            self._music_pending_search_query = None
                            fetched = int(job.params.get("limit") or self._music_search_page_size)
                            if self._music_track_count < fetched:
                                self._music_search_more_exhausted = True
                            self._music_update_scroll_footer()
                        else:
                            self._search_query = self._pending_search_query
                            self._pending_search_query = None
                            if len(self.results) < self._search_page_size:
                                self._search_more_exhausted = True
                            self._update_search_scroll_footer()
                        self._schedule_scroll_bottom_check()
                    elif ctx == "music":
                        self._music_search_query = None
                    else:
                        self._search_query = None
                elif job.state == FAILED:
                    if ctx == "music" and job.kind == "search":
                        self._music_stream_job_id = None
                    hdr = (
                        self.music_results_header_label
                        if ctx == "music"
                        else self.results_header_label
                    )
                    hdr.configure(text=f"Results — {job.kind} failed")
                    self._set_status(f"{job.kind.capitalize()} failed: {job.error}")
                elif job.state == CANCELLED:
                    if ctx == "music" and job.kind == "search":
                        self._music_stream_job_id = None
                    hdr = (
                        self.music_results_header_label
                        if ctx == "music"
                        else self.results_header_label
                    )
                    hdr.configure(text=f"Results — {job.kind} cancelled")
                    self._set_status(f"{job.kind.capitalize()} cancelled.")

            elif job.kind == "source_resolve":
                ctx = job.params.get("results_context", "download")
                if ctx != "music":
                    pass
                elif job.state == DONE and isinstance(job.result, list):
                    from .sources.base import MusicTrack

                    self.music_tracks = [
                        t if isinstance(t, MusicTrack) else MusicTrack.from_dict(t)
                        for t in job.result
                    ]
                    self.music_results = []
                    self._music_showing_tracks = True
                    self._music_search_query = None
                    self._music_render_results()
                    self._set_status(
                        f"Resolved {len(self.music_tracks)} track(s).",
                    )
                    if self._music_auto_download and self.music_tracks:
                        cookies = self.settings.get("cookies_path") or None
                        job = self.jobs.enqueue(
                            kind="source_match_all",
                            label=f"Match {len(self.music_tracks)} track(s) on YouTube",
                            tracks=[t.to_dict() for t in self.music_tracks],
                            cookies_path=cookies,
                            results_context="music",
                            auto_download=True,
                            **self._music_search_job_params(),
                        )
                        self._music_match_job_id = job.id
                elif job.state == FAILED:
                    self._music_auto_download = False
                    self._music_pending_out_dir = None
                    self.music_results_header_label.configure(
                        text="Results — resolve failed",
                    )
                    self._set_status(f"Resolve failed: {job.error}")
                elif job.state == CANCELLED:
                    self._music_auto_download = False
                    self._music_pending_out_dir = None

            elif job.kind == "source_match_all":
                ctx = job.params.get("results_context", "download")
                if ctx != "music":
                    pass
                elif job.state == DONE and isinstance(job.result, list):
                    self._music_match_job_id = None
                    from .sources.base import MusicTrack

                    self.music_tracks = [
                        t if isinstance(t, MusicTrack) else MusicTrack.from_dict(t)
                        for t in job.result
                    ]
                    self._music_showing_tracks = True
                    self._music_render_results()
                    matched = sum(1 for t in self.music_tracks if t.is_downloadable())
                    self._set_status(
                        f"Matched {matched}/{len(self.music_tracks)} track(s) on YouTube.",
                    )
                    if job.params.get("auto_download") and self._music_pending_out_dir:
                        self._music_enqueue_matched_downloads(self._music_pending_out_dir)
                        self._music_auto_download = False
                        self._music_pending_out_dir = None
                elif job.state == FAILED:
                    self._music_match_job_id = None
                    self._music_auto_download = False
                    self._music_pending_out_dir = None
                    self.music_results_header_label.configure(
                        text="Results — match failed",
                    )
                    self._set_status(f"Match failed: {job.error}")
                elif job.state == CANCELLED:
                    self._music_match_job_id = None
                    self._music_auto_download = False
                    self._music_pending_out_dir = None

            # "Load more" completion: append only the new tail of results.
            elif job.kind == "search_more":
                ctx = job.params.get("results_context", "download")
                if ctx == "music":
                    self._music_search_loading_more = False
                    self._music_clear_loading_indicator()
                else:
                    self._search_loading_more = False
                    self._clear_loading_indicator()
                if job.state == DONE and job.result is not None:
                    if isinstance(job.result, dict):
                        if ctx == "music":
                            new_tracks = list(job.result.get("tracks") or [])
                            new_albums = list(job.result.get("albums") or [])
                            if job.result.get("albums_exhausted"):
                                self._music_albums_exhausted = True
                            if job.result.get("tracks_exhausted"):
                                self._music_search_more_exhausted = True
                            if new_tracks or new_albums:
                                self._music_append_more_results(new_tracks, new_albums)
                                self._music_track_count += len(new_tracks)
                                self._music_album_count += len(new_albums)
                                parts = []
                                if new_tracks:
                                    parts.append(f"{len(new_tracks)} song(s)")
                                if new_albums:
                                    parts.append(f"{len(new_albums)} album(s)")
                                self._set_status(
                                    f"Loaded {' and '.join(parts)} "
                                    f"(total {len(self.music_results)}).",
                                )
                            else:
                                self._music_search_more_exhausted = True
                                self._set_status("No more results.")
                        else:
                            new_items = list(job.result.get("tracks") or [])
                            if job.result.get("tracks_exhausted"):
                                self._search_more_exhausted = True
                            if new_items:
                                self._append_more_results(new_items)
                                self._set_status(
                                    f"Loaded {len(new_items)} more "
                                    f"(total {len(self.results)}).",
                                )
                            else:
                                self._search_more_exhausted = True
                                self._set_status("No more results.")
                    elif isinstance(job.result, list):
                        already = int(job.params.get("already_loaded", 0))
                        new_items = job.result[already:]
                        if new_items:
                            if ctx == "music":
                                self._music_append_more_results(new_items)
                                self._music_track_count += sum(
                                    1 for r in new_items if r.kind == "track"
                                )
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
                if ctx == "music":
                    self._music_update_scroll_footer()
                else:
                    self._update_search_scroll_footer()
                self._schedule_scroll_bottom_check()
            else:
                # Download/embed: add to recent (deduped by video/song)
                self._add_recent_job(job)
                if job.state == FAILED:
                    self._set_status(f"[fail] {job.label}: {job.error}")
                elif job.state == CANCELLED:
                    self._set_status(f"[cancel] {job.label}")
                else:
                    self._set_status(f"[done] {job.label}")

            self.jobs.clear_recent()  # let JobQueue drop its terminal copies

        if job.kind != "search_more":
            if job.is_active:
                self._summary_jobs[job.id] = (job.label, job.progress_pct or 0.0)
            else:
                self._summary_jobs.pop(job.id, None)
            count = len(self._summary_jobs)
            self.tabs.buttons["active"].configure(text=f"↓   Downloads  {count}" if count else "↓   Downloads")
            if count:
                label, pct = next(iter(self._summary_jobs.values()))
                self._set_status(f"{_truncate(label, 48)} · {count} active")
                self._summary_progress.move_to(pct / 100)
            else:
                self._summary_progress.move_to(0)

        self._finish_import(job)
        self._update_empty_states()

        # Update headers
        self.active_header.configure(text=f"Active ({len(self._active_rows)})")
        self._update_recent_header()

        self._maybe_log_job_progress(job)

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

    def _hook_results_canvas_scroll(self, canvas) -> None:
        """Fire pagination checks when the results list is scrolled any way."""
        try:
            orig_cmd = canvas.cget("yscrollcommand")
        except Exception:  # noqa: BLE001
            return

        def _wrapped(first, last, _orig=orig_cmd) -> None:
            if _orig:
                canvas.tk.call(_orig, first, last)
            self._schedule_scroll_bottom_check()

        try:
            canvas.configure(yscrollcommand=_wrapped)
        except Exception:  # noqa: BLE001
            pass

    def _setup_scroll_forwarding(self) -> None:
        import os
        # Diagnostic logging is opt-in: set YTDLP_SCROLL_DEBUG=1 to get
        # per-event logs on stderr and at /tmp/ytdlp_scroll_debug.log.
        debug = bool(os.environ.get("YTDLP_SCROLL_DEBUG"))
        debug_path = "/tmp/ytdlp_scroll_debug.log" if debug else None
        if debug:
            try:
                with open(debug_path, "w") as f:
                    f.write("")
            except Exception:  # noqa: BLE001
                pass

        def _dlog(msg: str) -> None:
            if not debug:
                return
            print(msg, file=sys.stderr, flush=True)
            try:
                with open(debug_path, "a") as f:
                    f.write(msg + "\n")
            except Exception:  # noqa: BLE001
                pass

        # macOS "natural scrolling" inverts the sign of dy that Tk receives.
        # We detect the system preference once at startup and let Settings
        # ("scroll_direction") override it manually. A signed multiplier of
        # +1 means "matches macOS Notes/Safari", -1 means "inverted".
        self._scroll_sign = _detect_scroll_sign(self.settings)

        # Collect the inner canvases that we want to scroll on wheel/touchpad.
        self._scroll_canvases: list = []
        for frame in (
            self.results_frame,
            self.music_results_frame,
            self.active_frame,
            self.recent_frame,
            getattr(self, "_settings_scroll_frame", None),
            self.playlists_page.songs,
            self.playlists_page.results,
            self.identify_page.results,
            self.tabs.playlist_children,
        ):
            if frame is None:
                continue
            try:
                from .ui import coalesce_scrollbar_updates
                coalesce_scrollbar_updates(frame)
                self._scroll_canvases.append(frame._parent_canvas)  # noqa: SLF001
            except AttributeError:
                pass

        self._results_scroll_canvases: set = set()
        for frame in (self.results_frame, self.music_results_frame):
            try:
                canvas = frame._parent_canvas  # noqa: SLF001
                self._results_scroll_canvases.add(canvas)
                self._hook_results_canvas_scroll(canvas)
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
                before = canvas.yview()
                canvas.yview_scroll(units, "units")
                if canvas in self._results_scroll_canvases:
                    self._schedule_scroll_bottom_check()
                if debug:
                    after = canvas.yview()
                    def _recheck(c=canvas, b=before, a=after, u=units):
                        try:
                            now = c.yview()
                            _dlog(
                                f"[scroll]   units={u:+d} "
                                f"before={b[0]:.3f} after={a[0]:.3f} "
                                f"now={now[0]:.3f}"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    self.after(80, _recheck)
            except Exception:  # noqa: BLE001
                pass
            return "break"

        # Detect whether <TouchpadScroll> is supported (Tk 9+ on macOS/Aqua).
        # If yes, we bind ONLY TouchpadScroll on this app and unbind any
        # MouseWheel handlers (including CTk's) so we don't get paired,
        # opposite-direction events from a single trackpad tick.
        try:
            patchlevel = self.tk.call("info", "patchlevel")
            major = int(str(patchlevel).split(".", 1)[0])
        except Exception:  # noqa: BLE001
            major = 8
        use_touchpad_only = (sys.platform == "darwin" and major >= 9)

        def _on_mousewheel(event):
            canvas = _find_target_canvas(event.widget)
            _dlog(
                f"[scroll] MouseWheel t={event.time} delta={event.delta} "
                f"num={getattr(event,'num',0)} "
                f"widget={event.widget.__class__.__name__} "
                f"canvas={'yes' if canvas else 'no'}"
            )
            if canvas is None:
                return None
            num = getattr(event, "num", 0)
            if num == 4:
                return _scroll_units(canvas, -3 * self._scroll_sign)
            if num == 5:
                return _scroll_units(canvas, 3 * self._scroll_sign)
            delta = getattr(event, "delta", 0) or 0
            try:
                delta = int(delta)
            except (TypeError, ValueError):
                return "break"
            if delta == 0:
                return "break"
            if sys.platform == "darwin":
                step = max(-6, min(6, -delta * self._scroll_sign))
                return _scroll_units(canvas, step)
            return _scroll_units(canvas, -int(delta / 120) * 3 * self._scroll_sign)

        def _on_touchpad_scroll(event):
            # Tk 9 / macOS packs `(dx, dy)` into a single int as
            #   delta = (dx << 16) | (dy & 0xFFFF)
            # with each component being a 16-bit signed pixel delta.
            # Older docs claim event.delta is just dy; that's wrong on
            # Tk 9 macOS, where horizontal scroll capability moved the
            # encoding to two-axis.
            canvas = _find_target_canvas(event.widget)
            raw = int(getattr(event, "delta", 0) or 0)
            dy = raw & 0xFFFF
            if dy >= 0x8000:
                dy -= 0x10000
            dx = (raw >> 16) & 0xFFFF
            if dx >= 0x8000:
                dx -= 0x10000
            _dlog(
                f"[scroll] TouchpadScroll raw={raw} dx={dx} dy={dy} "
                f"widget={event.widget.__class__.__name__} "
                f"canvas={'yes' if canvas else 'no'}"
            )
            if canvas is None or dy == 0:
                return "break" if canvas is not None else None
            # Convert pixel dy to canvas units. macOS xscrollincrement=8 so
            # ~1 unit per 8 px feels right; clamp so big momentum bursts
            # don't fling the view.
            magnitude = max(1, min(8, abs(dy) // 4 or 1))
            # On macOS scrollingDeltaY: positive dy means the user's fingers
            # moved UP (with natural scrolling on, that scrolls the page
            # DOWN — content stays under the fingers). We want yview_scroll
            # positive (= scroll DOWN) for "fingers moved up" by default.
            units = magnitude if dy > 0 else -magnitude
            units *= self._scroll_sign
            return _scroll_units(canvas, units)

        def _dump_bindings(label: str) -> None:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                        "<TouchpadScroll>"):
                try:
                    cur = self.tk.call("bind", "all", seq)
                except Exception as e:  # noqa: BLE001
                    cur = f"<err {e}>"
                _dlog(f"[scroll]   {label} all/{seq}: {cur[:200]!r}")
            # Also dump Canvas class bindings — Tk Aqua may have built-in
            # scroll handling on the Canvas class itself.
            for seq in ("<MouseWheel>", "<TouchpadScroll>"):
                try:
                    cur = self.tk.call("bind", "Canvas", seq)
                except Exception as e:  # noqa: BLE001
                    cur = f"<err {e}>"
                _dlog(f"[scroll]   {label} Canvas/{seq}: {cur[:200]!r}")

        if debug:
            _dump_bindings("BEFORE unbind")

        # Wipe every existing scroll-related binding (CTk's plus anything
        # else) so we have full control over what scrolls.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                    "<TouchpadScroll>"):
            try:
                self.unbind_all(seq)
            except Exception:  # noqa: BLE001
                pass

        if debug:
            _dump_bindings("AFTER unbind")

        if use_touchpad_only:
            # macOS Tk 9+: trackpad gestures AND mouse wheels both fire
            # <TouchpadScroll>. Bind only that to avoid paired-event twitch.
            self.bind_all("<TouchpadScroll>", _on_touchpad_scroll)
            if debug:
                print(
                    f"[scroll] Tk {patchlevel} on darwin/aqua — TouchpadScroll only",
                    file=sys.stderr, flush=True,
                )
        else:
            self.bind_all("<MouseWheel>", _on_mousewheel)
            self.bind_all("<Button-4>", _on_mousewheel, add="+")
            self.bind_all("<Button-5>", _on_mousewheel, add="+")
            if debug:
                print(
                    f"[scroll] Tk {patchlevel} — MouseWheel/Button-4/5 only",
                    file=sys.stderr, flush=True,
                )

    def _bind_results_mousewheel(self, widget) -> None:
        # Kept as a public hook for places that previously called it
        # (loading indicator, result rows). With the global bind_all in
        # `_setup_scroll_forwarding`, no per-widget binding is needed.
        return

    # ====================== Misc UI =========================================

    def _set_status(self, text: str) -> None:
        self.status_var.set(text[:160])

    def _log_widget_pinned(self, widget: ctk.CTkTextbox) -> bool:
        try:
            return float(widget.yview()[1]) >= 0.95
        except Exception:  # noqa: BLE001
            return True

    def _bind_log_scroll_tracking(self, widget: ctk.CTkTextbox) -> None:
        def _on_scroll(_event=None) -> None:
            self.after_idle(self._update_log_latest_btn)

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                    "<KeyPress>", "<Button-1>"):
            widget.bind(seq, _on_scroll, add="+")

    def _update_log_latest_btn(self) -> None:
        embedded_pinned = self._log_widget_pinned(self.log_box)
        popout_pinned = True
        popout_btn = None
        if self._log_popout is not None:
            try:
                if self._log_popout.winfo_exists():
                    popout_pinned = self._log_widget_pinned(
                        self._log_popout.textbox,
                    )
                    popout_btn = self._log_popout._latest_btn
            except Exception:  # noqa: BLE001
                self._log_popout = None

        show_embedded = (
            not embedded_pinned
            and not self._activity_collapsed
            and self._activity_segment == "log"
        )
        if show_embedded:
            self._log_latest_btn.pack(
                side="right", padx=2, before=self._log_size_btn,
            )
        else:
            self._log_latest_btn.pack_forget()

        if popout_btn is not None:
            if popout_pinned:
                popout_btn.pack_forget()
            else:
                popout_btn.pack(side="right", padx=2)

    def _log_jump_to_latest(self) -> None:
        for widget in (self.log_box,):
            widget.configure(state="normal")
            widget.see("end")
            widget.configure(state="disabled")
        if self._log_popout is not None:
            try:
                if self._log_popout.winfo_exists():
                    self._log_popout.jump_to_latest()
            except Exception:  # noqa: BLE001
                pass
        self._update_log_latest_btn()

    def _append_log_line(self, text: str, widget: ctk.CTkTextbox) -> None:
        pinned = self._log_widget_pinned(widget)
        widget.configure(state="normal")
        widget.insert("end", text + "\n")
        if pinned:
            widget.see("end")
        widget.configure(state="disabled")

    def _log(self, text: str) -> None:
        self._append_log_line(text, self.log_box)
        if self._log_popout is not None:
            try:
                if self._log_popout.winfo_exists():
                    self._log_popout.append(text)
            except Exception:  # noqa: BLE001
                self._log_popout = None
        self.after_idle(self._update_log_latest_btn)

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        if self._log_popout is not None:
            try:
                if self._log_popout.winfo_exists():
                    self._log_popout.clear()
            except Exception:  # noqa: BLE001
                pass
        self._last_logged.clear()
        self._last_log_time.clear()
        self._update_log_latest_btn()

    def _maybe_log_job_progress(self, job: Job) -> None:
        if job.is_terminal:
            if job.state == FAILED:
                line = f"[failed] {job.label}: {job.error or job.progress_msg}"
            elif job.state == CANCELLED:
                line = f"[cancelled] {job.label}"
            else:
                line = f"[done] {job.label}"
            self._log(line)
            self._last_logged.pop(job.id, None)
            self._last_log_time.pop(job.id, None)
            return

        if not job.progress_msg:
            return

        if job.progress_msg.startswith("[downloading]"):
            now = time.monotonic()
            last_t = self._last_log_time.get(job.id, 0.0)
            if now - last_t < 1.0:
                return
            self._last_log_time[job.id] = now

        if self._last_logged.get(job.id) == job.progress_msg:
            return

        self._last_logged[job.id] = job.progress_msg
        self._log(f"[{job.state}] {job.label}: {job.progress_msg}")

    def _cycle_log_height(self) -> None:
        idx = _LOG_HEIGHT_CYCLE.index(self._log_height)
        self._log_height = _LOG_HEIGHT_CYCLE[(idx + 1) % len(_LOG_HEIGHT_CYCLE)]
        self.settings.set("panel_log_height", self._log_height)
        self._log_size_btn.configure(
            text=_LOG_HEIGHT_LABELS.get(self._log_height, "Size"),
        )
        font = ctk.CTkFont(size=13 if self._log_height != "normal" else 12)
        self.log_box.configure(font=font)
        if not self._activity_collapsed and self._activity_segment == "log":
            self._apply_activity_dock_layout()

    def _cycle_active_height(self) -> None:
        cycle = list(_LOG_HEIGHT_CYCLE)
        cur = str(self.settings.get("panel_active_height") or "normal")
        if cur not in cycle:
            cur = "normal"
        idx = cycle.index(cur)
        nxt = cycle[(idx + 1) % len(cycle)]
        self.settings.set("panel_active_height", nxt)
        self._active_size_btn.configure(text=nxt.capitalize())
        if not self._activity_collapsed and self._activity_segment == "active":
            self._apply_activity_dock_layout()

    def _cycle_recent_height(self) -> None:
        cycle = list(_LOG_HEIGHT_CYCLE)
        cur = str(self.settings.get("panel_recent_height") or "normal")
        if cur not in cycle:
            cur = "normal"
        idx = cycle.index(cur)
        nxt = cycle[(idx + 1) % len(cycle)]
        self.settings.set("panel_recent_height", nxt)
        self._recent_size_btn.configure(text=nxt.capitalize())
        if not self._activity_collapsed and self._activity_segment == "recent":
            self._apply_activity_dock_layout()

    def _toggle_active_popout(self) -> None:
        if getattr(self, "_active_popout", None) is not None:
            try:
                if self._active_popout.winfo_exists():
                    self._active_popout.focus()
                    return
            except Exception:  # noqa: BLE001
                self._active_popout = None
        self._active_popout = _JobsPopout(self, title="Active downloads", kind="active")

    def _toggle_recent_popout(self) -> None:
        if getattr(self, "_recent_popout", None) is not None:
            try:
                if self._recent_popout.winfo_exists():
                    self._recent_popout.focus()
                    return
            except Exception:  # noqa: BLE001
                self._recent_popout = None
        self._recent_popout = _JobsPopout(self, title="Recent", kind="recent")

    def _toggle_log_popout(self) -> None:
        if self._log_popout is not None:
            try:
                if self._log_popout.winfo_exists():
                    self._log_popout.focus()
                    return
            except Exception:  # noqa: BLE001
                self._log_popout = None
        content = self.log_box.get("1.0", "end").strip()
        self._log_popout = _LogPopout(self, initial_text=content)

    def _retry_job(self, job: Job) -> None:
        params = copy.deepcopy(job.params)
        self.jobs.enqueue(kind=job.kind, label=job.label, **params)

    def _retry_all_failed_recent(self) -> None:
        failed_jobs: list[Job] = []
        for row in self._recent_rows.values():
            failed_jobs.extend(row.failed_jobs())
        if not failed_jobs:
            return
        for job in failed_jobs:
            self._retry_job(job)
        self._set_status(f"Retrying {len(failed_jobs)} failed job(s)…")

    def _update_recent_header(self) -> None:
        total = len(self._recent_rows)
        failed = sum(
            1 for r in self._recent_rows.values() if r.has_failed()
        )
        cancelled = sum(
            1 for r in self._recent_rows.values() if r.is_all_cancelled()
        )
        ok = total - failed - cancelled
        if total == 0:
            text = "Recent (0)"
        elif failed or cancelled:
            bits = [f"Recent ({total})"]
            if failed:
                bits.append(f"{failed} failed")
            if ok:
                bits.append(f"{ok} ok")
            if cancelled:
                bits.append(f"{cancelled} cancelled")
            text = " — ".join(bits)
        else:
            text = f"Recent ({total})"
        self.recent_header.configure(text=text)
        # Retry btn only when Recent segment actions are visible.
        if (
            failed
            and not self._activity_collapsed
            and self._activity_segment == "recent"
        ):
            self._recent_retry_all_btn.pack(side="right", padx=2)
        else:
            self._recent_retry_all_btn.pack_forget()

    def _reorder_recent_rows(self) -> None:
        rows = list(self._recent_rows.values())
        state_order = {FAILED: 0, CANCELLED: 1, DONE: 2}
        rows.sort(
            key=lambda r: (
                state_order.get(r.worst_state(), 3),
                -r.latest_job_id,
            ),
        )
        for row in rows:
            row.frame.pack_forget()
        for row in rows:
            row.frame.pack(fill="x", padx=4, pady=2)

    def _clear_recent(self) -> None:
        for row in self._recent_rows.values():
            row.frame.destroy()
        self._recent_rows.clear()
        self._update_recent_header()

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
        if hasattr(self, "_audio_preview"):
            self._audio_preview.close()
        try:
            w = max(1080, int(self.winfo_width()))
            h = max(720, int(self.winfo_height()))
            self.settings.set("window_width", w)
            self.settings.set("window_height", h)
        except (TypeError, ValueError, Exception):  # noqa: BLE001
            pass
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

_ALT_THUMB_SIZE = (96, 54)


class _PreviewButton(ctk.CTkButton):
    """Poll worker events on Tk's thread; stop when the owning row disappears."""

    def __init__(self, parent, app, url, *, direct=False, catalog_track=None):
        super().__init__(parent, text="▶", width=34, command=self._toggle)
        self.app = app
        self.url = url
        self.direct = direct
        self.catalog_track = catalog_track
        self.token = None
        if not hasattr(app, "_audio_preview"):
            from .preview import AudioPreview
            app._audio_preview = AudioPreview()
            app._preview_button = None
        Tooltip(self, "Listen to an Apple catalog sample" if direct else
                "Play Apple catalog sample (metadata match). Right-click to check the selected YouTube source." if catalog_track else
                "Listen to the first 30 seconds of this YouTube result")
        if catalog_track is not None:
            def menu(event):
                app._show_popup_menu(self, [
                    ('Play Apple catalog sample (default)', lambda: self._toggle(catalog=True)),
                    ('Play selected YouTube source', lambda: self._toggle(catalog=False)),
                ])
                return 'break'
            self.bind('<Button-3>', menu)
            self.bind('<Button-2>', menu)
            self.bind('<Control-Button-1>', menu)

    def destroy(self):
        if self.app._preview_button is self:
            self.app._audio_preview.stop()
            self.app._preview_button = None
        super().destroy()

    def _toggle(self, *, catalog=None):
        if catalog is None:
            catalog = self.catalog_track is not None
        preview = self.app._audio_preview
        old = self.app._preview_button
        if old is self:
            preview.stop()
            self.app._preview_button = None
            self.configure(text="▶")
            self.app._set_status("Preview stopped")
            return
        if old is not None and old.winfo_exists():
            old.configure(text="▶")
        self.app._preview_button = self
        if catalog and self.catalog_track is not None:
            self.token = preview.play(self.url, catalog_track=self.catalog_track)
        else:
            self.token = preview.play(self.url, direct=True) if self.direct else preview.play(self.url, self.app.settings.get("cookies_path") or None)
        self.configure(text="…")
        self.app._set_status("Loading a 30-second preview… Click again to stop.")
        self.after(100, self._poll)

    def _poll(self):
        if self.app._preview_button is not self:
            return
        while True:
            try:
                token, state, message = self.app._audio_preview.events.get_nowait()
            except queue.Empty:
                break
            if token != self.token:
                continue
            self.app._set_status(message)
            self.configure(text="■" if state == "playing" else "…" if state == "loading" else "▶")
            if state not in ("playing", "loading"):
                self.app._preview_button = None
                return
        self.after(100, self._poll)


class _MusicAlternateResultRow:
    """Compact YouTube result row inside the alternate-match picker."""

    def __init__(
        self,
        parent,
        result: SearchResult,
        panel: "_MusicAlternatePanel",
    ) -> None:
        self._alive = True
        self.frame = ctk.CTkFrame(parent)
        self.frame.pack(fill="x", padx=2, pady=2)

        self._ctk_image = ctk.CTkImage(
            light_image=thumbcache.placeholder(_ALT_THUMB_SIZE),
            dark_image=thumbcache.placeholder(_ALT_THUMB_SIZE),
            size=_ALT_THUMB_SIZE,
        )
        self.thumb_label = ArtworkLabel(
            self.frame, text="", image=self._ctk_image,
            width=_ALT_THUMB_SIZE[0], height=_ALT_THUMB_SIZE[1],
        )
        self.thumb_label.pack(side="left", padx=(4, 6), pady=4)

        text_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True, padx=2, pady=4)
        title_row = ctk.CTkFrame(text_col, fg_color="transparent")
        title_row.pack(fill="x")
        rating_badge = result.content_rating_badge()
        if rating_badge:
            ctk.CTkLabel(
                title_row,
                text=rating_badge,
                width=16,
                anchor="center",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=(
                    ("#3d7a4a", "#7eb888") if rating_badge == "C"
                    else ("#9a4a4a", "#c88888")
                ),
            ).pack(side="left", padx=(0, 4))
        ctk.CTkLabel(
            title_row, text=result.display_title(80),
            anchor="w", font=ctk.CTkFont(weight="bold"),
            wraplength=420, justify="left",
        ).pack(side="left", fill="x", expand=True)
        meta = result.metadata_line()
        if meta:
            ctk.CTkLabel(
                text_col, text=meta, anchor="w", text_color=MUTED,
                wraplength=360, justify="left",
            ).pack(fill="x")

        ctk.CTkButton(
            self.frame, text="Use this", width=90,
            command=lambda: panel.apply_result(result),
        ).pack(side="right", padx=6, pady=4)

        ctk.CTkButton(
            self.frame, text="Open link", width=80,
            command=lambda: panel.app._open_media_link(result.url),
            fg_color="transparent", text_color=TEXT, hover_color=HOVER,
        ).pack(side="right", padx=2, pady=4)
        _PreviewButton(self.frame, panel.app, result.url).pack(side="right", padx=2, pady=4)

        if result.thumbnail_url:
            thumbcache.load(result.thumbnail_url, self._on_thumb)

    def _on_thumb(self, img) -> None:
        if not self._alive or img is None:
            return
        try:
            resized = img.resize(_ALT_THUMB_SIZE)
            self._ctk_image.configure(
                light_image=resized, dark_image=resized, size=_ALT_THUMB_SIZE,
            )
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        self._alive = False
        try:
            self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass


class _MusicAlternatePanel:
    """YouTube search picker for manually choosing a track match.

    When ``fill_parent`` is True, expands to fill the Music results body
    (rematch focus workspace). Otherwise packs as a compact inline panel.
    """

    def __init__(
        self,
        parent,
        index: int,
        app: "App",
        *,
        track: MusicTrack | None = None,
        current: SearchResult | None = None,
        fill_parent: bool = False,
    ) -> None:
        self.app = app
        self.index = index
        self.track = track
        self.current = current
        self._rematch_mode = "track" if track is not None else "search"
        self._fill_parent = fill_parent
        self._current_url = ""
        if track is not None and track.youtube_url:
            self._current_url = track.youtube_url
        elif current is not None:
            self._current_url = current.url
        self._alive = True
        self._rows: list[_MusicAlternateResultRow] = []

        self.frame = ctk.CTkFrame(parent, fg_color=PANEL)
        if fill_parent:
            self.frame.pack(fill="both", expand=True, padx=4, pady=4)
            self.frame.grid_columnconfigure(0, weight=1)
            self.frame.grid_rowconfigure(5, weight=1)
        else:
            self.frame.pack(fill="x", padx=8, pady=(0, 4))

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        if fill_parent:
            header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        else:
            header.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(
            header, text="Pick alternate YouTube match",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            header, text="Close", width=70,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=lambda: app._music_close_rematch(),
        ).pack(side="right")
        if self._current_url:
            _PreviewButton(header, app, self._current_url).pack(side="right", padx=4)
            ctk.CTkButton(
                header, text="Open link", width=100,
                fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
                command=lambda: app._open_media_link(self._current_url),
            ).pack(side="right", padx=4)

        current_label = ""
        if track is not None and track.youtube_title:
            current_label = track.youtube_title
            if track.youtube_uploader:
                current_label += f"  ·  {track.youtube_uploader}"
        elif current is not None:
            current_label = current.display_title()
            if current.uploader:
                current_label += f"  ·  {current.uploader}"
        if track is not None:
            src = track.display_title()
            src_lbl = ctk.CTkLabel(
                self.frame,
                text=f"Source: {src}",
                anchor="w",
                font=ctk.CTkFont(weight="bold"),
                wraplength=900, justify="left",
            )
            if fill_parent:
                src_lbl.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 2))
            else:
                src_lbl.pack(fill="x", padx=10, pady=(0, 2))
        if current_label:
            cur_lbl = ctk.CTkLabel(
                self.frame,
                text=f"Current: {current_label}",
                anchor="w", text_color=MUTED,
                wraplength=900, justify="left",
            )
            if fill_parent:
                cur_lbl.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
            else:
                cur_lbl.pack(fill="x", padx=10, pady=(0, 4))

        query_row = ctk.CTkFrame(self.frame, fg_color="transparent")
        if fill_parent:
            query_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
        else:
            query_row.pack(fill="x", padx=8, pady=(0, 4))
        if track is not None:
            default_query = " ".join(
                x for x in (track.artist, track.title) if x
            ).strip() or track.title
        elif current is not None:
            parsed = parse_youtube_track(current.title, current.uploader)
            default_query = " ".join(
                x for x in (parsed.artist, parsed.title) if x
            ).strip() or current.title
        else:
            default_query = ""
        self.query_var = ctk.StringVar(value=default_query)
        entry = ctk.CTkEntry(query_row, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind(
            "<Return>",
            lambda _e: app._music_search_alternate(
                index, self.query_var.get(), mode=self._rematch_mode,
            ),
        )
        ctk.CTkButton(
            query_row, text="Search", width=90,
            command=lambda: app._music_search_alternate(
                index, self.query_var.get(), mode=self._rematch_mode,
            ),
        ).pack(side="left")

        self.status_label = ctk.CTkLabel(
            self.frame, text="Searching YouTube…",
            anchor="w", text_color=MUTED,
        )
        if fill_parent:
            self.status_label.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 2))
            self.results_frame = ctk.CTkScrollableFrame(self.frame)
            self.results_frame.grid(
                row=5, column=0, sticky="nsew", padx=6, pady=(0, 8),
            )
        else:
            self.status_label.pack(fill="x", padx=10, pady=(0, 4))
            self.results_frame = ctk.CTkScrollableFrame(self.frame, height=200)
            self.results_frame.pack(fill="x", padx=6, pady=(0, 8))

        app._music_alternate_panels[index] = self
        app.after(
            50,
            lambda: app._music_search_alternate(
                index, default_query, mode=self._rematch_mode,
            ),
        )

    def set_searching(self, msg: str) -> None:
        if not self._alive:
            return
        self.status_label.configure(text=msg or "Searching…")

    def set_error(self, msg: str) -> None:
        if not self._alive:
            return
        self.status_label.configure(text=f"Error: {msg}")

    def set_results(self, results: list[SearchResult]) -> None:
        if not self._alive:
            return
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        if self._current_url:
            results = [r for r in results if r.url != self._current_url]
        if not results:
            self.status_label.configure(text="No results — try a different query.")
            return
        self.status_label.configure(
            text=f"{len(results)} result(s) — click Use this to match",
        )
        for result in results:
            self._rows.append(
                _MusicAlternateResultRow(self.results_frame, result, self),
            )

    def apply_result(self, result: SearchResult) -> None:
        if self._rematch_mode == "track":
            self.app._music_apply_manual_match(self.index, result)
        else:
            self.app._music_apply_search_alternate(self.index, result)

    def destroy(self) -> None:
        self._alive = False
        self.app._music_alternate_panels.pop(self.index, None)
        if self.app._music_rematch_panel is self:
            self.app._music_rematch_panel = None
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        try:
            self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass


class _MusicTrackRow:
    """One row for an imported track (Spotify, etc.) before/after YouTube match."""

    def __init__(
        self,
        parent,
        track: MusicTrack,
        app: "App",
        *,
        track_index: int,
    ) -> None:
        self.track = track
        self.app = app
        self.track_index = track_index
        self._alive = True
        self._thumb_size = (56, 56)

        self.outer = ctk.CTkFrame(parent, fg_color="transparent")
        self.outer.pack(fill="x", padx=4, pady=2)

        border_kw: dict[str, Any] = {}
        if track.match_status == MATCH_FAILED:
            border_kw = {"border_width": 2, "border_color": ("#c44", "#f55")}

        self.frame = ctk.CTkFrame(self.outer, fg_color=ROW if track_index % 2 else SURFACE, corner_radius=4, **border_kw)
        self.frame.pack(fill="x")

        thumb_url = track.cover_url or track.thumbnail_url
        self._ctk_image = ctk.CTkImage(
            light_image=thumbcache.placeholder(self._thumb_size),
            dark_image=thumbcache.placeholder(self._thumb_size),
            size=self._thumb_size,
        )
        self.thumb_label = ArtworkLabel(
            self.frame, text="", image=self._ctk_image,
            width=self._thumb_size[0], height=self._thumb_size[1],
        )
        self.thumb_label.pack(side="left", padx=(6, 8), pady=6)

        btn_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        btn_col.pack(side="right", padx=6, pady=4)

        if track.is_downloadable():
            more_btn = ctk.CTkButton(
                btn_col, text="⋯", width=36,
                fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
                command=lambda: self._show_row_menu(),
            )
            more_btn.pack(side="right", padx=2)
            self._more_btn = more_btn
            ctk.CTkButton(
                btn_col, text="Download", width=100,
                fg_color="transparent", text_color=ACCENT, hover_color=HOVER,
                command=lambda: app._music_download_one_track(track, override=False),
            ).pack(side="right", padx=2)
        elif track.match_status == MATCH_PENDING:
            self._more_btn = None
            ctk.CTkLabel(
                btn_col, text="Match first", text_color=MUTED,
            ).pack(side="right", padx=8)
        else:
            self._more_btn = None
            ctk.CTkButton(
                btn_col, text="Pick match", width=100,
                command=lambda: app._music_toggle_alternate(track_index),
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                btn_col, text="Retry", width=80,
                fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
                command=lambda: app._music_retry_track(track_index),
            ).pack(side="right", padx=2)

        text_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True, padx=2, pady=6)
        title_label = ctk.CTkLabel(
            text_col, text=track.display_title(68),
            anchor="w", font=ctk.CTkFont(size=13),
            wraplength=0, justify="left",
        )
        title_label.pack(fill="x")
        Tooltip(title_label, f"{track.artist} — {track.title}")
        meta = track.metadata_line()
        if meta:
            color = MUTED
            if track.match_status == MATCH_FAILED:
                color = ("#a33", "#f66")
            ctk.CTkLabel(
                text_col, text=meta, anchor="w", text_color=color,
                wraplength=0, justify="left",
            ).pack(fill="x")

        app._bind_results_mousewheel(self.frame)
        if thumb_url:
            self._kick_off_thumb_fetch(thumb_url)

    def _show_row_menu(self) -> None:
        items: list[tuple[str, Callable[[], None]]] = [
            ("Choose another match…", lambda: self.app._music_toggle_alternate(self.track_index)),
            ("View match", lambda: self.app._music_show_match(
                self.track, self.track_index,
            )),
            ("Download to folder…", lambda: self.app._music_download_one_track(
                self.track, override=True,
            )),
        ]
        anchor = self._more_btn or self.frame
        self.app._show_playlist_download_menu(anchor, items, lambda p: self.app._download_to_playlist(p, track=self.track))

    def _kick_off_thumb_fetch(self, url: str) -> None:
        def _on_loaded(img) -> None:
            if not self._alive or img is None:
                return

            def _apply() -> None:
                if not self._alive:
                    return
                try:
                    resized = artwork_image(img, self._thumb_size)
                    self._ctk_image.configure(
                        light_image=resized, dark_image=resized, size=self._thumb_size,
                    )
                except Exception:  # noqa: BLE001
                    pass

            try:
                self.app.after(0, _apply)
            except Exception:  # noqa: BLE001
                pass

        thumbcache.load(url, _on_loaded)

    def _apply_thumb(self, img) -> None:
        if not self._alive or img is None:
            return
        try:
            resized = artwork_image(img, self._thumb_size)
            self._ctk_image.configure(
                light_image=resized, dark_image=resized, size=self._thumb_size,
            )
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        self._alive = False
        try:
            self.outer.destroy()
        except Exception:  # noqa: BLE001
            pass


class _ResultRow:
    """One row in the results list: thumbnail | title+meta | Download / 📁."""

    def __init__(
        self,
        parent,
        result: SearchResult,
        app: "App",
        *,
        mode: str = "download",
        result_index: int = 0,
    ) -> None:
        self.result = result
        self.mode = mode
        self.app = app
        self._alive = True
        self._thumb_size = (56, 56) if mode == "music" else (100, 56)

        if mode == "music":
            if result.kind in ("album", "playlist"):
                download_fn = lambda: app._music_download_collection(result, override=False)
                folder_fn = lambda: app._music_download_collection(result, override=True)
                btn_text = "Download album" if result.kind == "album" else "Download playlist"
            else:
                download_fn = lambda: app._music_download_one(result, override=False)
                folder_fn = lambda: app._music_download_one(result, override=True)
                btn_text = "Download"
            self.outer = ctk.CTkFrame(parent, fg_color="transparent")
            self.outer.pack(fill="x", padx=4, pady=2)
            row_parent = self.outer
        else:
            download_fn = lambda: app._download_one(result, override=False)
            folder_fn = lambda: app._download_one(result, override=True)
            btn_text = "Download"
            row_parent = parent

        self.frame = ctk.CTkFrame(row_parent, fg_color=ROW if result_index % 2 else SURFACE, corner_radius=4)
        self.frame.pack(fill="x", padx=(0 if mode == "music" else 4), pady=(0 if mode == "music" else 2))

        # Thumbnail (left).
        self._ctk_image = ctk.CTkImage(
            light_image=thumbcache.placeholder(self._thumb_size),
            dark_image=thumbcache.placeholder(self._thumb_size),
            size=self._thumb_size,
        )
        self.thumb_label = ArtworkLabel(
            self.frame, text="", image=self._ctk_image,
            width=self._thumb_size[0], height=self._thumb_size[1],
        )
        self.thumb_label.pack(side="left", padx=(6, 8), pady=6)

        # Pack buttons first so long titles/artists can't steal their space.
        btn_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        btn_col.pack(side="right", padx=6, pady=4)
        self._download_btn: ctk.CTkButton | None = None
        self._folder_btn: ctk.CTkButton | None = None
        self._collection_busy = False
        self._download_btn_label = btn_text
        menu_items = [("Download to folder…", folder_fn)]
        if mode == "music" and result.kind == "track":
            menu_items.insert(0, ("Choose another match…", lambda: app._music_toggle_alternate(result_index)))
        self._folder_btn = ctk.CTkButton(
            btn_col, text="⋯", width=32, height=32,
            command=lambda: app._show_playlist_download_menu(self._folder_btn, menu_items, lambda p: app._download_to_playlist(p, result=self.result)) if mode == "music" and result.kind == "track" else app._show_popup_menu(self._folder_btn, menu_items),
            fg_color="transparent", text_color=MUTED, hover_color=HOVER,
        )
        Tooltip(self._folder_btn, "More actions")
        self._folder_btn.pack(side="right", padx=2)
        dl_width = 140 if mode == "music" and result.kind in ("album", "playlist") else 110
        self._download_btn = ctk.CTkButton(
            btn_col, text=btn_text, width=dl_width, command=download_fn,
            fg_color="transparent", text_color=ACCENT, hover_color=HOVER,
        )
        self._download_btn.pack(side="right", padx=2)
        self._open_link_btn = ctk.CTkButton(
            btn_col, text="Open link", width=80,
            command=lambda: app._open_media_link(self.result.url),
            fg_color="transparent", text_color=TEXT, hover_color=HOVER,
        )
        self._open_link_btn.pack(side="right", padx=2)
        Tooltip(self._open_link_btn, "Open in your browser without preparing an audio preview")
        self._preview_btn = None
        if result.kind == "track":
            from .playlists import PlaylistTrack
            from .metadata.parse import parse_youtube_track
            parsed = parse_youtube_track(result.title, result.uploader)
            catalog_track = PlaylistTrack('', parsed.title, parsed.artist, duration_s=result.duration_s or 0) if mode == 'music' else None
            self._preview_btn = _PreviewButton(btn_col, app, result.url, catalog_track=catalog_track)
            self._preview_btn.pack(side="right", padx=2)
        if (
            mode == "music"
            and result.kind in ("album", "playlist")
            and (result.url or "").strip() in app._pending_collection_urls
        ):
            self.set_collection_busy(True)

        # Title + metadata (center).
        text_col = ctk.CTkFrame(self.frame, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True, padx=2, pady=6)

        title_row = ctk.CTkFrame(text_col, fg_color="transparent")
        title_row.pack(fill="x")
        if result.kind in ("album", "playlist"):
            badge = result.kind.upper()
            ctk.CTkLabel(
                title_row,
                text=badge,
                width=78,
                anchor="w",
                text_color=("gray30", "gray70"),
            ).pack(side="left", padx=(0, 6))
        rating_badge = result.content_rating_badge()
        if rating_badge:
            ctk.CTkLabel(
                title_row,
                text=rating_badge,
                width=16,
                anchor="center",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=(
                    ("#3d7a4a", "#7eb888") if rating_badge == "C"
                    else ("#9a4a4a", "#c88888")
                ),
            ).pack(side="left", padx=(0, 4))
        title_label = ctk.CTkLabel(
            title_row,
            text=result.display_title(68),
            anchor="w",
            font=ctk.CTkFont(size=13),
            wraplength=0,
            justify="left",
        )
        title_label.pack(side="left", fill="x", expand=True)
        Tooltip(title_label, result.title + "\n" + result.metadata_line())
        meta = self._metadata_text(result)
        self._meta_label: ctk.CTkLabel | None = None
        if meta:
            self._meta_label = ctk.CTkLabel(
                text_col, text=meta, anchor="w",
                text_color=MUTED,
                wraplength=0, justify="left",
            )
            self._meta_label.pack(fill="x")

        # Forward mouse-wheel events from every child widget up to the
        # scrollable frame's canvas — otherwise the wheel does nothing once
        # the pointer is over a label/button/thumbnail.
        app._bind_results_mousewheel(self.frame)

        # Kick off the thumbnail fetch. The cache callback may fire on a
        # worker thread, so we hop back to the Tk main loop via `after`.
        self._kick_off_thumb_fetch()

    def _metadata_text(self, result: SearchResult) -> str:
        if self.mode == "music" and result.kind == "track":
            return "  ·  ".join(bit for bit in (
                result.uploader, _format_duration(result.duration_s) if result.duration_s else "",
            ) if bit)
        return result.metadata_line()

    def set_collection_busy(self, busy: bool) -> None:
        """Show Expanding… and disable buttons while an album/playlist resolves."""
        if not self._alive:
            return
        if self.result.kind not in ("album", "playlist"):
            return
        self._collection_busy = busy
        if self._download_btn is not None:
            try:
                if busy:
                    self._download_btn.configure(
                        text="Expanding…", state="disabled",
                    )
                else:
                    self._download_btn.configure(
                        text=self._download_btn_label, state="normal",
                    )
            except Exception:  # noqa: BLE001
                pass
        if self._folder_btn is not None:
            try:
                self._folder_btn.configure(
                    state="disabled" if busy else "normal",
                )
            except Exception:  # noqa: BLE001
                pass

    def update_result(self, result: SearchResult) -> None:
        if not self._alive:
            return
        old_thumb = self.result.thumbnail_url
        if self._preview_btn is not None:
            if self._preview_btn.url != result.url and self.app._preview_button is self._preview_btn:
                self._preview_btn._toggle()
            self._preview_btn.url = result.url
        self.result = result
        meta = self._metadata_text(result)
        if self._meta_label is not None:
            if meta:
                self._meta_label.configure(text=meta)
            else:
                self._meta_label.configure(text="")
        if result.thumbnail_url and result.thumbnail_url != old_thumb:
            self._kick_off_thumb_fetch()

    def _kick_off_thumb_fetch(self) -> None:
        url = self.result.thumbnail_url

        def _on_loaded(img) -> None:
            if not self._alive or img is None:
                return

            def _apply() -> None:
                if not self._alive:
                    return
                try:
                    resized = artwork_image(img, self._thumb_size)
                    self._ctk_image.configure(
                        light_image=resized, dark_image=resized, size=self._thumb_size,
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Always hop to the next UI tick — even cache hits — so row
            # construction isn't blocked by PIL resizes.
            try:
                self.app.after(0, _apply)
            except Exception:  # noqa: BLE001
                pass

        thumbcache.load(url, _on_loaded)

    def _apply_thumb(self, img) -> None:
        if not self._alive or img is None:
            return
        try:
            resized = artwork_image(img, self._thumb_size)
            self._ctk_image.configure(
                light_image=resized, dark_image=resized, size=self._thumb_size,
            )
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        self._alive = False
        try:
            if hasattr(self, "outer"):
                self.outer.destroy()
            else:
                self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass


class _LogPopout(ctk.CTkToplevel):
    """Detached log window sharing the main app's log stream."""

    def __init__(self, app: "App", *, initial_text: str = "") -> None:
        super().__init__(app)
        self.app = app
        self.title("easy-dlp — Log")
        self.geometry("900x500")
        self.minsize(500, 300)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(6, 2))
        self._latest_btn = ctk.CTkButton(
            bar, text="↓ Latest", width=80,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self.jump_to_latest,
        )
        ctk.CTkButton(
            bar, text="Clear", width=70,
            command=self._clear,
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            bar, text="Close", width=70,
            command=self._close,
        ).pack(side="right", padx=2)

        font = ctk.CTkFont(size=13)
        self.textbox = ctk.CTkTextbox(self, wrap="none", font=font)
        self.textbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.textbox.configure(state="disabled")
        self.app._bind_log_scroll_tracking(self.textbox)

        if initial_text:
            self.textbox.configure(state="normal")
            self.textbox.insert("1.0", initial_text + "\n")
            self.textbox.see("end")
            self.textbox.configure(state="disabled")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self.lift)

    def append(self, text: str) -> None:
        self.app._append_log_line(text, self.textbox)
        self.app.after_idle(self.app._update_log_latest_btn)

    def jump_to_latest(self) -> None:
        self.textbox.configure(state="normal")
        self.textbox.see("end")
        self.textbox.configure(state="disabled")
        self._latest_btn.pack_forget()
        self.app.after_idle(self.app._update_log_latest_btn)

    def clear(self) -> None:
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")

    def _clear(self) -> None:
        self.app._clear_log()

    def _close(self) -> None:
        self.app._log_popout = None
        self.destroy()


class _JobsPopout(ctk.CTkToplevel):
    """Detached Active/Recent window that mirrors main job lists."""

    def __init__(self, app: "App", *, title: str, kind: str) -> None:
        super().__init__(app)
        self.app = app
        self.kind = kind  # "active" | "recent"
        self.title(f"easy-dlp — {title}")
        self.geometry("900x420")
        self.minsize(500, 260)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(bar, text=title, font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(bar, text="Close", width=80, command=self._close).pack(side="right")

        self.body = ctk.CTkScrollableFrame(self)
        self.body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._refresh()
        self.after(500, self._poll_refresh)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self.lift)

    def _poll_refresh(self) -> None:
        if not self.winfo_exists():
            return
        self._refresh()
        self.after(750, self._poll_refresh)

    def _refresh(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        jobs = self.app.jobs.active() if self.kind == "active" else self.app.jobs.recent(50)
        for job in jobs:
            row = ctk.CTkFrame(self.body)
            row.pack(fill="x", padx=4, pady=3)
            ctk.CTkLabel(row, text=f"[{job.state}] {job.label}", anchor="w").pack(
                side="left", fill="x", expand=True, padx=8, pady=6,
            )
            ctk.CTkLabel(row, text=job.progress_msg or "", anchor="e").pack(
                side="right", padx=8, pady=6,
            )

    def _close(self) -> None:
        if self.kind == "active":
            self.app._active_popout = None
        else:
            self.app._recent_popout = None
        self.destroy()


class _MatchDetailDialog(ctk.CTkToplevel):
    """Show source track vs YouTube match for one playlist track."""

    def __init__(self, app: "App", track: MusicTrack, track_index: int) -> None:
        super().__init__(app)
        self.app = app
        self.track_index = track_index
        self.title("YouTube match")
        self.geometry("560x320")
        self.resizable(True, True)

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(
            body, text="Source track", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).pack(fill="x")
        ctk.CTkLabel(
            body, text=track.display_title(), anchor="w", justify="left",
            wraplength=520,
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            body, text="YouTube match", anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).pack(fill="x")
        yt_title = track.youtube_title or track.title
        yt_artist = track.youtube_uploader or track.artist
        ctk.CTkLabel(
            body, text=f"{yt_artist} — {yt_title}", anchor="w",
            justify="left", wraplength=520,
        ).pack(fill="x", pady=(0, 4))

        url_box = ctk.CTkTextbox(body, height=48, wrap="word")
        url_box.pack(fill="x", pady=(0, 8))
        url_box.insert("1.0", track.youtube_url or "")
        url_box.configure(state="disabled")

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x")
        _PreviewButton(body, app, track.youtube_url, catalog_track=track).pack(anchor="w", pady=6)
        ctk.CTkButton(
            btn_row, text="Open in browser", width=130,
            command=lambda: app._open_media_link(track.youtube_url or ""),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Copy link", width=100,
            command=lambda: app.clipboard_clear()
            or app.clipboard_append(track.youtube_url or "")
            or app._set_status("Link copied"),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            btn_row, text="Change match", width=110,
            fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
            command=self._change_match,
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            btn_row, text="Close", width=80,
            command=self.destroy,
        ).pack(side="right")

        self.after(100, self.lift)

    def _change_match(self) -> None:
        self.destroy()
        self.app._music_open_rematch(self.track_index, mode="track")


class _MatchReviewDialog(ctk.CTkToplevel):
    """Scrollable table of all matched tracks for quick manual review."""

    def __init__(self, app: "App", tracks: list[MusicTrack]) -> None:
        super().__init__(app)
        self.app = app
        self.title("Review matches")
        self.geometry("980x600")
        self.minsize(700, 400)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 4))
        matched = sum(1 for t in tracks if t.youtube_url)
        ctk.CTkLabel(
            header,
            text=f"{matched} matched track(s) — preview audio to verify",
            anchor="w", font=ctk.CTkFont(weight="bold"),
        ).pack(side="left")

        scroll = ctk.CTkScrollableFrame(self)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        for i, track in enumerate(tracks):
            if not track.youtube_url:
                continue
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)

            ctk.CTkLabel(
                row, text=track.display_title(70), anchor="w",
                width=280, justify="left",
            ).pack(side="left", padx=(4, 8))

            yt_label = (
                f"{track.youtube_uploader or '?'} — "
                f"{_truncate(track.youtube_title or track.title, 45)}"
            )
            ctk.CTkLabel(
                row, text=yt_label, anchor="w",
                text_color=("gray30", "gray75"), width=380, justify="left",
            ).pack(side="left", fill="x", expand=True, padx=4)

            _PreviewButton(row, app, track.youtube_url, catalog_track=track).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="Open link", width=80,
                command=lambda u=track.youtube_url: app._open_media_link(u or ""),
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="Copy", width=60,
                command=lambda u=track.youtube_url: app.clipboard_clear()
                or app.clipboard_append(u or "")
                or app._set_status("Link copied"),
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                row, text="View", width=60,
                fg_color="transparent", border_width=0, text_color=TEXT, hover_color=HOVER,
                command=lambda idx=i, t=track: _MatchDetailDialog(app, t, idx),
            ).pack(side="right", padx=2)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(footer, text="Close", width=80,
                      command=self.destroy).pack(side="right")

        self.after(100, self.lift)


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
                                   text_color=MUTED,
                                   width=380)
        self.status.pack(side="left")

    def update(self, job: Job) -> None:
        self.label.configure(text=job.label)
        self.bar.set(job.progress_pct / 100.0 if job.progress_pct else 0.0)
        msg = job.progress_msg or job.state
        self.status.configure(text=_truncate(msg, 120))


class _RecentRow:
    _GLYPH = {DONE: "✓", FAILED: "✗", CANCELLED: "—"}
    _GLYPH_COLOR = {
        DONE: ("#1a7a1a", "#4ade4a"),
        FAILED: ("#a33", "#f66"),
        CANCELLED: ("gray40", "gray60"),
    }

    def __init__(self, parent, job: Job, app: "App") -> None:
        self.app = app
        self.jobs: list[Job] = [job]
        self.kinds: list[str] = [job.kind]
        self.job = job
        self.latest_job_id = job.id

        border_kw: dict[str, Any] = {}
        if job.state == FAILED:
            border_kw = {"border_width": 2, "border_color": ("#c44", "#f55")}
        self.frame = ctk.CTkFrame(parent, **border_kw)
        self.frame.pack(fill="x", padx=4, pady=2)

        inner = ctk.CTkFrame(self.frame, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=4)

        self._glyph_label = ctk.CTkLabel(
            inner, text="·", width=20,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self._glyph_label.pack(side="left", padx=(0, 6))

        text_col = ctk.CTkFrame(inner, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True)
        self.title_label = ctk.CTkLabel(
            text_col, text="", anchor="w", justify="left",
            font=ctk.CTkFont(weight="bold"), wraplength=700,
        )
        self.title_label.pack(fill="x")
        self.meta_label = ctk.CTkLabel(
            text_col, text="", anchor="w", justify="left",
            text_color=MUTED, wraplength=700,
        )
        self.meta_label.pack(fill="x")
        self.error_label = ctk.CTkLabel(
            text_col, text="", anchor="w", justify="left",
            text_color=("#a33", "#f66"), wraplength=700,
        )

        self.btn_col = ctk.CTkFrame(inner, fg_color="transparent")
        self.btn_col.pack(side="right", padx=(4, 0))
        self._refresh()

    def merge(self, job: Job) -> None:
        self.jobs.append(job)
        if job.kind not in self.kinds:
            self.kinds.append(job.kind)
        if job.id > self.latest_job_id:
            self.latest_job_id = job.id
            self.job = job
        self._refresh()

    def latest_jobs_by_kind(self) -> list[Job]:
        """Latest attempt per kind — ignores superseded retries."""
        by_kind: dict[str, Job] = {}
        for j in self.jobs:
            prev = by_kind.get(j.kind)
            if prev is None or j.id > prev.id:
                by_kind[j.kind] = j
        return list(by_kind.values())

    def has_failed(self) -> bool:
        return any(j.state == FAILED for j in self.latest_jobs_by_kind())

    def is_all_cancelled(self) -> bool:
        latest = self.latest_jobs_by_kind()
        return bool(latest) and all(j.state == CANCELLED for j in latest)

    def failed_jobs(self) -> list[Job]:
        return [
            j for j in self.latest_jobs_by_kind()
            if j.state in (FAILED, CANCELLED)
        ]

    def worst_state(self) -> str:
        if self.has_failed():
            return FAILED
        if self.is_all_cancelled():
            return CANCELLED
        return DONE

    def _refresh(self) -> None:
        state = self.worst_state()
        glyph = self._GLYPH.get(state, "·")
        glyph_color = self._GLYPH_COLOR.get(state, MUTED)
        self._glyph_label.configure(text=glyph, text_color=glyph_color)

        if state == FAILED:
            self.frame.configure(border_width=2, border_color=("#c44", "#f55"))
        else:
            self.frame.configure(border_width=0)

        self.title_label.configure(text=_recent_primary_title(self.job))
        meta = _recent_metadata_line(self.job, self.kinds)
        if meta:
            self.meta_label.configure(text=meta)
            self.meta_label.pack(fill="x")
        else:
            self.meta_label.pack_forget()

        errors = [
            f"{_RECENT_KIND_LABEL.get(j.kind, j.kind)}: {j.error}"
            for j in self.latest_jobs_by_kind()
            if j.state == FAILED and j.error
        ]
        if errors:
            self.error_label.configure(text="\n".join(errors))
            self.error_label.pack(fill="x")
        else:
            self.error_label.pack_forget()

        for child in self.btn_col.winfo_children():
            child.destroy()

        out_dir = self.job.params.get("output_dir")
        if state == DONE and out_dir:
            ctk.CTkButton(
                self.btn_col, text="📁", width=40,
                command=lambda: _reveal_in_file_manager(out_dir),
            ).pack(side="right", padx=2)

        for failed_job in self.failed_jobs():
            kind_label = _RECENT_KIND_LABEL.get(failed_job.kind, "Retry")
            ctk.CTkButton(
                self.btn_col, text=f"Retry {kind_label}", width=90,
                command=lambda j=failed_job: self.app._retry_job(j),
            ).pack(side="right", padx=2)


# ============================================================================
# Generic path row used by Settings (incl. legacy Embed Thumbnail)
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
