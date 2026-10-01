"""Shared song actions for identification and playlist recommendations."""
import queue
from urllib.parse import urlparse, urlencode
import customtkinter as ctk
from .ui import MUTED, TEXT, HOVER, ArtworkLabel, Tooltip, artwork_image, ROW, SURFACE
from . import song_table, thumbcache


class DiscoveryRow(ctk.CTkFrame):
    def __init__(self, parent, app, track, on_similar, *, because='', on_identify=None, row_index=0):
        super().__init__(parent, fg_color=ROW if row_index % 2 else SURFACE, corner_radius=4)
        self.track = track
        self.pack(fill='x', pady=4)
        summary = ctk.CTkFrame(self, fg_color='transparent')
        summary.pack(fill='x')
        song_table.columns(summary, action_width=0)
        self.cells = song_table.cells(summary, track)
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
        if on_identify is not None:
            bind_identify_menu(self.similar_btn, app, lambda: on_identify(track))
        from .gui import _PreviewButton
        self.preview_btn = _PreviewButton(actions, app, track.preview_url, direct=True)
        if not track.preview_url:
            self.preview_btn.configure(state='disabled')
            Tooltip(self.preview_btn, 'No catalog preview available')
        self.preview_btn.pack(side='left', padx=2)
        self.download_btn = ctk.CTkButton(actions, text='Download', width=105,
            command=self._queue_download)
        self.download_btn.pack(side='left', padx=2)
        Tooltip(self.download_btn, 'Queue matching and download in the background using your Music settings')
        self._download_after = None
        key = track.apple_id or track.key or (track.artist, track.title)
        self._download_job = getattr(app, '_discovery_jobs', {}).get(key)
        self._app = app
        if self._download_job is not None:
            self._poll_download()
        self.more_btn = ctk.CTkButton(actions, text='⋯', width=34, command=self._open_menu,
                                         fg_color='transparent', text_color=TEXT, hover_color=HOVER)
        self.more_btn.pack(side='right', padx=2)
        Tooltip(self.more_btn, 'Download to an Apple Music playlist or open in another app')

    def _open_menu(self):
        youtube = 'https://www.youtube.com/results?' + urlencode({'search_query': f'{self.track.artist} {self.track.title}'})
        job = self._download_job
        if job is not None and job.params.get('url'):
            youtube = job.params['url']
        items = [(label, lambda value=url: self._app._open_media_link(value))
                 for label, url in [('Open in Apple Music', self.track.apple_url),
                                    ('Open in Shazam', self.track.url), ('Open in YouTube', youtube)]
                 if urlparse(url).scheme in ('http', 'https')]
        self._app._show_playlist_download_menu(self.more_btn, items, self._queue_download)

    def _queue_download(self, playlist=None):
        job = self._app._discovery_download(self.track, playlist)
        if job is not None:
            if self._download_after is not None:
                self.after_cancel(self._download_after)
                self._download_after = None
            self._download_job = job
            self._poll_download()

    def _poll_download(self):
        from .jobs import DONE, FAILED, CANCELLED
        self._download_after = None
        job = self._download_job
        if job.state == DONE:
            skipped = isinstance(job.result, dict) and job.result.get('skipped_duplicate')
            self.download_btn.configure(text='Already saved' if skipped else 'Downloaded', state='disabled')
        elif job.state in (FAILED, CANCELLED):
            self.download_btn.configure(text='Retry download', state='normal')
        else:
            self.download_btn.configure(text='Queued', state='disabled')
            self._download_after = self.after(250, self._poll_download)

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
        if self._download_after is not None:
            self.after_cancel(self._download_after)
        if self._cover_after is not None:
            self.after_cancel(self._cover_after)
        super().destroy()


class DiscoveryRenderer:
    """Yield to Tk between small batches, and discard superseded renders."""
    def __init__(self, parent, app, on_similar, on_identify=None):
        self.parent, self.app, self.on_similar = parent, app, on_similar
        self.on_identify = on_identify
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
        self._pending = iter(enumerate(items))
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
            index, (track, because) = item
            DiscoveryRow(self.parent, self.app, track, self.on_similar, because=because, on_identify=self.on_identify, row_index=index)
        self._after = self.parent.after(10, self._batch)


def bind_identify_menu(button, app, callback):
    def menu(event):
        app._show_popup_menu(button, [('Identify audio, then find similar', callback)])
        return 'break'
    for sequence in ('<Button-3>', '<Button-2>', '<Control-Button-1>'):
        button.bind(sequence, menu)
    Tooltip(button, 'Right-click or Control-click to identify audio before finding similar songs')
