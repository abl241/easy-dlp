"""Read-only playlist browser with song and playlist discovery."""
import queue
import threading
import unicodedata
from tkinter import filedialog

import customtkinter as ctk

from . import discovery, playlists, recommendations, thumbcache
from .discovery_widgets import DiscoveryRow
from .preview import _safe_error
from .ui import MUTED, TEXT, HOVER, Tooltip, ArtworkLabel
from .playlist_artwork import ArtworkLoader, SIZE
from . import song_table


class PlaylistsPage(ctk.CTkFrame):
    PAGE_SIZE = 50

    def __init__(self, parent, app):
        super().__init__(parent, fg_color='transparent')
        self.app = app
        self._busy = False
        self._closed = False
        self._cancel = threading.Event()
        self._events = queue.Queue()
        self._art_events = queue.Queue()
        self._art_targets = {}
        self._art_serial = 0
        self._artwork = ArtworkLoader()
        self._art_generation = self._artwork.new_view()
        self._track_cache = {}
        self._attempted_load = False
        app.tabs.playlist_open_command = self._on_sidebar_open
        app.tabs.set_playlist_items([], self._select)
        self._playlists = {}
        self._tracks = []
        self._filtered_tracks = []
        self._selected_name = ''
        self._shown = 0
        self._filter_after = None
        self._more = None
        self._selected_row = None
        self.pack(fill='both', expand=True)
        ctk.CTkLabel(self, text='Playlists', font=ctk.CTkFont(size=28, weight='bold')).pack(anchor='w')
        ctk.CTkLabel(self, text='Browse your Music playlists and discover songs to try next. Your library stays unchanged.',
                     anchor='w', text_color=MUTED, wraplength=750, justify='left').pack(fill='x', pady=(0, 10))
        toolbar = ctk.CTkFrame(self, fg_color='transparent')
        toolbar.pack(fill='x')
        self.load_btn = ctk.CTkButton(toolbar, text='Load Apple Music', command=self._load_library)
        self.load_btn.pack(side='left', padx=(0, 6))
        self.import_btn = ctk.CTkButton(toolbar, text='Import playlist XML…', command=self._import)
        self.import_btn.pack(side='left', padx=4)
        self.artwork_btn = ctk.CTkButton(toolbar, text='Artwork folder…', width=130, command=self._choose_artwork_folder)
        self.artwork_btn.pack(side='left', padx=4)
        Tooltip(self.artwork_btn, 'Use matching JPG/PNG files from a local folder. Embedded song artwork and adjacent covers are also checked.')
        self.cancel_btn = ctk.CTkButton(toolbar, text='Cancel', width=80, state='disabled', command=self._cancel_work)
        self.cancel_btn.pack(side='right')
        choose = ctk.CTkFrame(self, fg_color='transparent')
        choose.pack(fill='x', pady=10)
        self.playlist_title = ctk.CTkLabel(choose, text='Choose a playlist in the sidebar', anchor='w', font=ctk.CTkFont(size=16, weight='bold'))
        self.playlist_title.pack(side='left', fill='x', expand=True)
        self.recommend_btn = ctk.CTkButton(choose, text='Recommend for playlist', command=self._recommend, state='disabled')
        self.recommend_btn.pack(side='right', padx=(8, 0))
        self.status = ctk.CTkLabel(self, text='Load playlists from the macOS Music app, or import an exported XML playlist.',
                                  anchor='w', wraplength=800, justify='left')
        self.status.pack(fill='x', pady=4)
        self.views = ctk.CTkTabview(self)
        self.views.pack(fill='both', expand=True)
        songs_tab = self.views.add('Songs')
        self.song_filter = ctk.StringVar()
        self.filter_entry = ctk.CTkEntry(songs_tab, textvariable=self.song_filter,
                     placeholder_text='Search this playlist by song, artist, or album')
        self.filter_entry.pack(fill='x', pady=(0, 6))
        self.filter_entry.bind('<Return>', lambda event: self._filter_songs())
        self.song_filter.trace_add('write', lambda *_: self._schedule_filter())
        self.filter_count = ctk.CTkLabel(songs_tab, text='', anchor='w', text_color=MUTED, height=20)
        self.filter_count.pack(fill='x')
        self.song_header = song_table.header(songs_tab)
        self.songs = ctk.CTkScrollableFrame(songs_tab)
        self.songs.pack(fill='both', expand=True)
        recommendations_tab = self.views.add('Recommendations')
        song_table.header(recommendations_tab, action_width=0)
        self.results = ctk.CTkScrollableFrame(recommendations_tab)
        self.results.pack(fill='both', expand=True)
        self._poll_id = self.after(100, self._poll)

    def _on_sidebar_open(self):
        if not self._attempted_load and not self._busy and not self._playlists:
            self._load_library()

    def _choose_artwork_folder(self):
        folder = filedialog.askdirectory(title='Choose a folder containing song thumbnails')
        if folder:
            self.app.settings.set('playlist_artwork_dir', folder)
            self._filter_songs()

    def _artwork_folders(self):
        return tuple(dict.fromkeys(folder for folder in (
            self.app.settings.get('playlist_artwork_dir'), self.app.settings.get('thumb_dir'),
        ) if folder))

    def _cancel_work(self):
        self._cancel.set()
        self.status.configure(text='Cancelling… waiting for the current request to finish.')

    def _start(self, kind, work, message):
        if self._busy:
            return
        self._busy = True
        self._cancel = threading.Event()
        for widget in (self.load_btn, self.import_btn, self.recommend_btn):
            widget.configure(state='disabled')
        self.app.tabs.set_playlists_busy(True)
        self.cancel_btn.configure(state='normal')
        self.status.configure(text=message)
        def run():
            try:
                self._events.put((kind, work(), None))
            except Exception as error:
                self._events.put((kind, None, _safe_error(str(error)) or 'Request failed. Try again.'))
        threading.Thread(target=run, name='playlist-discovery', daemon=True).start()

    def _load_library(self):
        self._attempted_load = True
        self._start('library', lambda: playlists.list_playlists(self._cancel), 'Reading playlists from Music… Allow Automation access if macOS asks.')

    def _import(self):
        path = filedialog.askopenfilename(title='Import an exported Music playlist', filetypes=[('Music XML', '*.xml'), ('All files', '*')])
        if path:
            self._start('library', lambda: playlists.import_xml(path), 'Reading exported playlists…')

    def _select(self, label):
        if self._busy or label not in self._playlists:
            return
        if label == self._selected_name:
            self.views.set('Songs')
            return
        playlist = self._playlists[label]
        self._start('tracks', lambda: (label, self._track_cache[label] if label in self._track_cache else playlists.load_tracks(playlist, self._cancel)), f'Loading {playlist.display_name}…')

    def _song(self, track, row=None):
        if self._busy:
            return
        if self._selected_row is not None and self._selected_row.winfo_exists():
            self._selected_row.configure(fg_color='transparent')
        self._selected_row = row
        if row is not None:
            row.configure(fg_color=HOVER)
        self._start('similar', lambda: recommendations.for_song(track, self._cancel), f'Finding similar to {track.artist} — {track.title}…')

    def _similar(self, track):
        if self._busy:
            return
        self._start('similar', lambda: (track, discovery.find_similar(track.key, self._cancel)), f'Finding similar to {track.artist} — {track.title}…')

    def _recommend(self):
        if not self._tracks:
            return
        tracks = tuple(self._tracks)
        self._start('recommend', lambda: recommendations.for_playlist(
            tracks, self._cancel, progress=lambda message: self._events.put(('progress', message, None))),
            f'Finding recommendations for {self._selected_name} from up to 8 evenly spaced songs…')

    def _clear(self, parent):
        for child in parent.winfo_children():
            child.destroy()

    def focus_filter(self):
        self.views.set('Songs')
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, 'end')

    def _schedule_filter(self):
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
        self._filter_after = self.after(75, self._filter_songs)

    def _filter_songs(self):
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
            self._filter_after = None
        self._art_generation = self._artwork.new_view()
        self._art_targets.clear()
        def normalize(text):
            return ''.join(c for c in unicodedata.normalize('NFKD', text).casefold() if not unicodedata.combining(c))
        terms = normalize(self.song_filter.get()).split()
        self._filtered_tracks = [track for track in self._tracks if all(
            term in normalize(f'{track.title} {track.artist} {track.album}') for term in terms)]
        self.filter_count.configure(text=f'{len(self._filtered_tracks)} of {len(self._tracks)} songs')
        self.songs._parent_canvas.yview_moveto(0)
        self._clear(self.songs)
        self._more = None
        self._selected_row = None
        self._shown = 0
        self._show_more()
        if not self._filtered_tracks:
            ctk.CTkLabel(self.songs, text='No songs match this filter.' if self._tracks else 'This playlist is empty.', text_color=MUTED).pack(pady=16)

    def _show_more(self):
        if self._more is not None:
            self._more.destroy()
            self._more = None
        end = min(self._shown + self.PAGE_SIZE, len(self._filtered_tracks))
        for track in self._filtered_tracks[self._shown:end]:
            row = ctk.CTkFrame(self.songs, fg_color='transparent')
            row.pack(fill='x', pady=1)
            song_table.columns(row)
            placeholder = thumbcache.placeholder(SIZE)
            image = ctk.CTkImage(light_image=placeholder, dark_image=placeholder, size=SIZE)
            artwork = ArtworkLabel(row, text='', image=image, width=SIZE[0], height=SIZE[1])
            artwork.grid(row=0, column=0, padx=(6, 8), pady=4)
            artwork.bind('<Button-1>', lambda event, t=track, r=row: self._song(t, r))
            generation = self._art_generation
            self._art_serial += 1
            token = self._art_serial
            self._art_targets[token] = (artwork, image)
            events = self._art_events
            # Never retain Tk widgets/images in a worker callback: their
            # finalizers must run on Tk's thread, including during shutdown.
            self._artwork.load(track, self._artwork_folders(), generation,
                               lambda loaded, g=generation, key=token: events.put((g, key, loaded)))
            ctk.CTkButton(row, text='Find similar', width=100, command=lambda t=track, r=row: self._song(t, r)).grid(row=0, column=5, padx=5, pady=6)
            row.cells = song_table.cells(row, track, lambda t=track, r=row: self._song(t, r))
        self._shown = end
        if end < len(self._filtered_tracks):
            self._more = ctk.CTkButton(self.songs, text=f'Show more ({end}/{len(self._filtered_tracks)})', command=self._show_more)
            self._more.pack(pady=8)

    def _poll(self):
        if self._closed:
            return
        for _ in range(20):
            try:
                generation, key, image = self._art_events.get_nowait()
            except queue.Empty:
                break
            destination = self._art_targets.pop(key, None)
            if image is not None and generation == self._art_generation and destination:
                widget, target = destination
                if widget.winfo_exists():
                    target.configure(light_image=image, dark_image=image)
        try:
            kind, result, error = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == 'progress':
                if not self._cancel.is_set():
                    self.status.configure(text=result)
                self._poll_id = self.after(100, self._poll)
                return
            self._busy = False
            self.load_btn.configure(state='normal')
            self.import_btn.configure(state='normal')
            self.cancel_btn.configure(state='disabled')
            if self._cancel.is_set():
                self.status.configure(text='Cancelled. Previous results are still available.')
            elif error:
                self.status.configure(text=error)
            elif kind == 'library':
                self._playlists = {f'{index + 1}. {p.display_name}': p for index, p in enumerate(result)}
                self._art_generation = self._artwork.new_view()
                self._art_targets.clear()
                self._track_cache = {}
                self._tracks = []
                self._filtered_tracks = []
                self._selected_name = ''
                self._selected_row = None
                self._more = None
                self._clear(self.songs)
                self._clear(self.results)
                self.app.tabs.set_playlist_items([(key, playlist.display_name) for key, playlist in self._playlists.items()], self._select)
                self.app.tabs.set_playlist_selection(None)
                self.playlist_title.configure(text='Choose a playlist in the sidebar' if result else 'No playlists found')
                self.app.tabs._set_playlists_expanded(True)
                self.status.configure(text=f'{len(result)} playlists loaded. Choose a playlist to browse its songs.' if result else 'No playlists found. Open Music and sync your library, or import a playlist XML.')

            elif kind == 'tracks':
                self._selected_name, self._tracks = result
                self._track_cache[self._selected_name] = self._tracks
                self.app.tabs.set_playlist_selection(self._selected_name)
                self.playlist_title.configure(text=self._playlists[self._selected_name].display_name)
                self._clear(self.songs)
                self._clear(self.results)
                self._more = None
                self._selected_row = None
                self._shown = 0
                self._filter_songs()
                self.views.set('Songs')
                self.status.configure(text=f'{len(self._tracks)} songs in {self._selected_name}. Click a song to find similar, or recommend for the playlist.')
            elif kind == 'similar':
                seed, related = result
                self._clear(self.results)
                for track in related:
                    DiscoveryRow(self.results, self.app, track, self._similar, because=f'Because you chose {seed.artist} — {seed.title}')
                self.views.set('Recommendations')
                self.status.configure(text=f'{len(related)} similar songs. Matched seed: {seed.artist} — {seed.title}.' if related else f'No related songs returned for {seed.title}.')
            elif kind == 'recommend':
                self._clear(self.results)
                if result.seeds:
                    ctk.CTkLabel(self.results, text='Seeds from ' + self._selected_name + ':\n' + '; '.join(result.seeds) +
                        '\nSame playlist order uses the same seeds. Songs suggested by more seeds rank higher.',
                        anchor='w', justify='left', wraplength=750, text_color=MUTED).pack(fill='x', padx=8, pady=8)
                for item in result.suggestions:
                    DiscoveryRow(self.results, self.app, item.track, self._similar, because='Related to: ' + '; '.join(item.because))
                if result.skipped:
                    ctk.CTkLabel(self.results, text='Skipped seeds:\n' + '\n'.join(_safe_error(line) for line in result.skipped),
                                 anchor='w', text_color=MUTED, wraplength=700, justify='left').pack(fill='x', padx=8, pady=10)
                self.views.set('Recommendations')
                self.status.configure(text=f'{len(result.suggestions)} suggestions from {result.matched}/{result.attempted} seed songs. Existing playlist songs and duplicates excluded; up to 3 songs per artist.')
            if not self._busy:
                self.app.tabs.set_playlists_busy(False)
                self.recommend_btn.configure(state='normal' if self._tracks else 'disabled')
        self._poll_id = self.after(100, self._poll)

    def destroy(self):
        self._closed = True
        if self._filter_after is not None:
            self.after_cancel(self._filter_after)
        self._artwork.close()
        self._art_targets.clear()
        self._cancel.set()
        self.after_cancel(self._poll_id)
        super().destroy()
