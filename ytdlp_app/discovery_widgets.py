"""Shared song actions for identification and playlist recommendations."""
import queue
from urllib.parse import urlparse
import customtkinter as ctk
from .ui import MUTED, TEXT, HOVER, ArtworkLabel, Tooltip, artwork_image
from . import song_table, thumbcache


class DiscoveryRow(ctk.CTkFrame):
    def __init__(self, parent, app, track, on_similar, *, because=''):
        super().__init__(parent)
        self.track = track
        self.pack(fill='x', pady=4)
        summary = ctk.CTkFrame(self, fg_color='transparent')
        summary.pack(fill='x')
        song_table.columns(summary, action_width=0)
        self.cells = song_table.cells(summary, track, lambda: on_similar(track))
        placeholder = thumbcache.placeholder((48, 48))
        self._image = ctk.CTkImage(light_image=placeholder, dark_image=placeholder, size=(48, 48))
        self.artwork = ArtworkLabel(summary, text='', image=self._image, width=48, height=48)
        self.artwork.grid(row=0, column=0, padx=7, pady=4)
        self._cover_events = queue.Queue()
        self._cover_after = None
        if track.artwork_url:
            thumbcache.load(track.artwork_url, self._cover_events.put)
            self._cover_after = self.after(100, self._poll_cover)
        details = list(track.genres)
        if track.release_date:
            details.append('Released ' + track.release_date)
        rating = {'explicit': 'Explicit', 'cleaned': 'Clean', 'notExplicit': 'Not explicit'}.get(track.explicitness)
        if rating:
            details.append(rating)
        if track.label:
            details.append(track.label)
        if track.track_number:
            details.append(f'Track {track.track_number}' + (f' · Disc {track.disc_number}' if track.disc_number else ''))
        if details:
            ctk.CTkLabel(self, text=' · '.join(details), anchor='w', text_color=MUTED,
                         wraplength=700, justify='left').pack(fill='x', padx=12)
        if because:
            ctk.CTkLabel(self, text=because, anchor='w', text_color=MUTED,
                         wraplength=650, justify='left').pack(fill='x', padx=12)
        actions = ctk.CTkFrame(self, fg_color='transparent')
        actions.pack(fill='x', padx=8, pady=6)
        self.similar_btn = ctk.CTkButton(actions, text='Find similar', width=100,
                                        command=lambda: on_similar(track))
        self.similar_btn.pack(side='left', padx=2)
        from .gui import _PreviewButton
        self.preview_btn = _PreviewButton(actions, app, track.preview_url, direct=True)
        if not track.preview_url:
            self.preview_btn.configure(state='disabled')
            Tooltip(self.preview_btn, 'No catalog preview available')
        self.preview_btn.pack(side='left', padx=2)
        self.download_btn = ctk.CTkButton(actions, text='Download…', width=105,
            command=lambda: app._discovery_download(track))
        self.download_btn.pack(side='left', padx=2)
        Tooltip(self.download_btn, 'Find a YouTube source, then review the match and download options in Music')
        for label, url in [('Apple Music', track.apple_url), ('Open Shazam', track.url)]:
            if urlparse(url).scheme in ('http', 'https'):
                ctk.CTkButton(actions, text=label, width=105, fg_color='transparent',
                             text_color=TEXT, hover_color=HOVER,
                             command=lambda value=url: app._open_media_link(value)).pack(side='left', padx=2)

    def _poll_cover(self):
        self._cover_after = None
        try:
            image = self._cover_events.get_nowait()
        except queue.Empty:
            self._cover_after = self.after(100, self._poll_cover)
            return
        if image is not None:
            image = artwork_image(image, (48, 48))
            self._image.configure(light_image=image, dark_image=image)

    def destroy(self):
        if self._cover_after is not None:
            self.after_cancel(self._cover_after)
        super().destroy()


class DiscoveryRenderer:
    """Yield to Tk between small batches, and discard superseded renders."""
    def __init__(self, parent, app, on_similar):
        self.parent, self.app, self.on_similar = parent, app, on_similar
        self._after = None
        self._pending = iter(())

    def cancel(self):
        if self._after is not None:
            self.parent.after_cancel(self._after)
            self._after = None
        self._pending = iter(())
        self._done = None

    def show(self, items, done=None):
        self.cancel()
        self._pending = iter(items)
        self._done = done
        self._batch()

    def _batch(self):
        self._after = None
        for _ in range(2):
            item = next(self._pending, None)
            if item is None:
                if self._done:
                    self._done()
                    self._done = None
                return
            track, because = item
            DiscoveryRow(self.parent, self.app, track, self.on_similar, because=because)
        self._after = self.parent.after(10, self._batch)
