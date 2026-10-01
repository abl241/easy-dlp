"""Compact, independently selected download destination with lazy Music reads."""
import queue
import threading
import time
import customtkinter as ctk
from . import playlists
from .ui import MUTED


class PlaylistDestination(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color='transparent')
        self.app = app
        self.targets = {}
        self._loaded = 0
        self._poll_id = None
        self._events = queue.Queue()
        self._cancel = threading.Event()
        ctk.CTkLabel(self, text='Add to playlist', text_color=MUTED).pack(side='left', padx=(0, 6))
        self.menu = ctk.CTkOptionMenu(self, values=['None'], width=150)
        self.menu.set('None')
        self.menu.pack(side='left')
        self._open = self.menu._open_dropdown_menu
        self.menu._open_dropdown_menu = self._open_menu

    def selected(self):
        return self.targets.get(self.menu.get())

    def set_playlists(self, items):
        selected = self.selected()
        self.targets = {f'{p.name} · {i+1}': p for i, p in enumerate(items)
                        if not p.special_kind and not p.is_smart and not p.id.startswith('xml:')}
        self.menu.configure(values=['None', *self.targets])
        self.menu.set(next((label for label, p in self.targets.items() if selected and p.id == selected.id), 'None'))
        self._loaded = time.monotonic()

    def _open_menu(self):
        if time.monotonic() - self._loaded < 60:
            self._open()
            return
        if self._poll_id is not None:
            return
        self.app._set_status('Loading Apple Music playlists…')
        events, cancel = self._events, self._cancel
        def work():
            try:
                events.put((playlists.list_playlists(cancel), None))
            except Exception as error:
                events.put((None, str(error)))
        threading.Thread(target=work, daemon=True, name='playlist-destination').start()
        self._poll_id = self.after(100, self._poll)

    def _poll(self):
        self._poll_id = None
        try:
            items, error = self._events.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(100, self._poll)
            return
        if error:
            self.app._set_status(error)
        else:
            self.set_playlists(items)
        self._open()

    def destroy(self):
        self._cancel.set()
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        super().destroy()
