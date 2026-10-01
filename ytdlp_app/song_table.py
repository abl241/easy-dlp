"""Shared Song / Artist / Album / Time alignment for library and discovery."""
import customtkinter as ctk
from .ui import MUTED, TEXT, Tooltip


def time_text(seconds):
    if not seconds or seconds < 0:
        return '—'
    seconds = int(seconds)
    return f'{seconds // 60}:{seconds % 60:02d}'


def columns(frame, action_width=110):
    frame.grid_columnconfigure(0, minsize=62)
    for column, weight in ((1, 3), (2, 2), (3, 2)):
        frame.grid_columnconfigure(column, weight=weight, uniform='song-data')
    frame.grid_columnconfigure(4, minsize=56)
    frame.grid_columnconfigure(5, minsize=action_width)


def header(parent, action_width=110):
    frame = ctk.CTkFrame(parent, fg_color='transparent')
    frame.pack(fill='x', padx=(0, 16), pady=(2, 4))
    columns(frame, action_width)
    for column, text in enumerate(('Song', 'Artist', 'Album', 'Time'), 1):
        ctk.CTkLabel(frame, text=text, width=1, anchor='w', text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight='bold')).grid(row=0, column=column, sticky='ew', padx=5)
    return frame


def cells(frame, track, callback=None):
    result = {}
    values = (('Song', track.title), ('Artist', track.artist or '—'),
              ('Album', track.album or '—'), ('Time', time_text(track.duration_s)))
    for column, (name, value) in enumerate(values, 1):
        label = ctk.CTkLabel(frame, text=value, width=1, anchor='w', text_color=TEXT,
                             cursor='hand2' if callback else '', font=ctk.CTkFont(size=13))
        label.grid(row=0, column=column, sticky='ew', padx=5, pady=8)
        if callback:
            label.bind('<Button-1>', lambda event: callback())
        Tooltip(label, value)
        result[name] = label
    return result
