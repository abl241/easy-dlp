"""Lightweight desktop presentation components; no network or background work."""

from __future__ import annotations

import time
import customtkinter as ctk
from PIL import ImageOps

# Blue/violet from the supplied note-and-arrow icon, adjusted for readable
# foregrounds. Solid surfaces approximate Music's materials without live blur.
ACCENT = ("#2368bc", "#64a5f4")
ACCENT_FILL = ("#286fc6", "#286fc6")
ACCENT_HOVER = ("#205eab", "#327cd5")
VIOLET = ("#7742bf", "#b28af3")
SURFACE = ("#ffffff", "#232629")
SIDEBAR = ("#f2f2f2", "#272b2e")
ROW = ("#f5f5f5", "#2b2e32")
INPUT = ("#eeeeee", "#1e2124")
PANEL = ("#ededed", "#303438")
MUTED = ("#646464", "#ababaf")
HOVER = ("#e3e3e3", "#3b3f42")
BORDER = ("#dedede", "#414448")
TEXT = ("#202020", "#eeeeef")


def artwork_image(image, size):
    """Preserve aspect ratio; square covers and video stills never stretch."""
    return ImageOps.pad(image.convert("RGB"), size, color="#232629")


class Tooltip:
    """A delayed full-text label, created only while it is needed."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.timer = self.window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def _schedule(self, _event=None):
        self.hide()
        self.timer = self.widget.after(650, self._show)

    def _show(self):
        self.timer = None
        if not self.widget.winfo_exists():
            return
        self.window = ctk.CTkToplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth() - 480)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window.geometry(f"+{max(0, x)}+{y}")
        ctk.CTkLabel(self.window, text=self.text, wraplength=450, justify="left",
                     fg_color=SIDEBAR, corner_radius=8).pack(padx=8, pady=6)

    def hide(self, _event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


def install_theme() -> None:
    """Extend CTk's bundled theme without introducing a runtime dependency."""
    ctk.set_default_color_theme("blue")
    theme = ctk.ThemeManager.theme
    theme["CTk"]["fg_color"] = SURFACE
    theme["CTkFrame"].update(fg_color=SURFACE, top_fg_color=SIDEBAR,
                             border_color=BORDER, corner_radius=10)
    theme["CTkButton"].update(fg_color=ACCENT_FILL, hover_color=ACCENT_HOVER,
                              corner_radius=8, border_width=0,
                              text_color=("#ffffff", "#ffffff"))
    theme["CTkLabel"]["text_color"] = TEXT
    theme["CTkEntry"].update(fg_color=INPUT, border_color=BORDER,
                            text_color=TEXT, placeholder_text_color=MUTED, corner_radius=9)
    for key in ("CTkCheckBox", "CTkRadioButton"):
        theme[key].update(fg_color=ACCENT_FILL, hover_color=ACCENT_HOVER, text_color=TEXT)
    theme["CTkSwitch"].update(fg_color=HOVER, progress_color=ACCENT_FILL, text_color=TEXT)
    theme["CTkSlider"].update(button_color=ACCENT, button_hover_color=ACCENT)
    theme["CTkSegmentedButton"].update(
        fg_color=SIDEBAR, selected_color=HOVER, selected_hover_color=HOVER,
        unselected_color=SIDEBAR, unselected_hover_color=HOVER, text_color=TEXT,
    )
    theme["CTkProgressBar"].update(progress_color=VIOLET, fg_color=HOVER)
    theme["CTkOptionMenu"].update(fg_color=HOVER, button_color=HOVER,
                                  button_hover_color=SIDEBAR, text_color=TEXT)
    theme["CTkTextbox"].update(fg_color=INPUT, border_color=BORDER, text_color=TEXT)
    theme["DropdownMenu"].update(fg_color=PANEL, hover_color=HOVER, text_color=TEXT)


def quiet_button(parent, **kwargs):
    return ctk.CTkButton(parent, fg_color="transparent", hover_color=HOVER,
                         text_color=TEXT, border_width=0, **kwargs)


class SearchField(ctk.CTkEntry):
    """CTk's native placeholder is disabled with StringVar; use a visual hint."""

    def __init__(self, parent, *, textvariable, placeholder_text, **kwargs):
        super().__init__(parent, textvariable=textvariable, **kwargs)
        self.variable = textvariable
        self.hint = ctk.CTkLabel(self, text=placeholder_text, text_color=MUTED,
                                fg_color=INPUT, height=20, anchor="w")
        self.hint.bind("<Button-1>", lambda _event: self.focus_set())
        self.bind("<FocusIn>", lambda _event: self.hint.place_forget(), add="+")
        self.bind("<FocusOut>", self._sync_hint, add="+")
        self._hint_trace = self.variable.trace_add("write", self._sync_hint)
        self._sync_hint()

    def _sync_hint(self, *_args):
        if not self.variable.get() and self.focus_get() not in (self, self._entry):
            self.hint.place(x=12, rely=0.5, anchor="w")
        else:
            self.hint.place_forget()

    def destroy(self):
        self.variable.trace_remove("write", self._hint_trace)
        super().destroy()


class SidebarPages(ctk.CTkFrame):
    """Retained pages keep selection and scroll position when navigating."""

    def __init__(self, parent, *, command, activity_command):
        super().__init__(parent, fg_color=SURFACE, corner_radius=0)
        self.command = command
        self.current = ""
        self.pages = {}
        self.buttons = {}
        self.sidebar = ctk.CTkFrame(self, width=174, fg_color=SIDEBAR, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        ctk.CTkLabel(self.sidebar, text="easy-dlp", anchor="w",
                     font=ctk.CTkFont(size=21, weight="bold")).pack(fill="x", padx=20, pady=(26, 0))
        ctk.CTkLabel(self.sidebar, text="YOUR MEDIA, SIMPLIFIED", anchor="w",
                     text_color=MUTED, font=ctk.CTkFont(size=10)).pack(fill="x", padx=20, pady=(0, 28))
        ctk.CTkLabel(self.sidebar, text="Library", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=20, pady=(0, 6))
        for name, label in (("Music", "♫   Music"), ("Video", "▷   Video")):
            self._add_button(name, label)
        ctk.CTkLabel(self.sidebar, text="Activity", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=20, pady=(26, 6))
        for key, label in (("active", "↓   Downloads"), ("recent", "◷   Recent")):
            btn = quiet_button(self.sidebar, text=label, anchor="w", height=38,
                               command=lambda k=key: activity_command(k))
            btn.pack(fill="x", padx=10, pady=2)
            self.buttons[key] = btn
        self._add_button("Settings", "Settings", bottom=True)
        self.content = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        self.content.pack(side="left", fill="both", expand=True, padx=20, pady=18)

    def _add_button(self, name, label, bottom=False):
        btn = quiet_button(self.sidebar, text=label, anchor="w", height=38,
                           command=lambda: self.set(name))
        btn.pack(side="bottom" if bottom else "top", fill="x", padx=10,
                 pady=14 if bottom else 2)
        self.buttons[name] = btn

    def add(self, name):
        page = ctk.CTkFrame(self.content, fg_color="transparent", corner_radius=0)
        self.pages[name] = page
        return page

    def set(self, name):
        if name == self.current:
            return
        if self.current:
            self.pages[self.current].pack_forget()
            self.buttons[self.current].configure(fg_color="transparent", text_color=TEXT)
        self.current = name
        self.pages[name].pack(fill="both", expand=True)
        self.buttons[name].configure(fg_color=HOVER, text_color=VIOLET)
        self.command()

    def get(self):
        return self.current


class SourcePanel(ctk.CTkFrame):
    """Always-visible search, with an optional bulk-import disclosure."""

    def __init__(self, parent, *, bulk_label="Import multiple links or a track list", **kwargs):
        kwargs.pop("height", None)
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.pages = {}
        self.current = "Search YouTube"
        self.bulk_label = bulk_label
        self.toggle = quiet_button(self, text=f"＋  {bulk_label}",
                                   anchor="w", height=28, command=self._toggle)

    def add(self, name):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self.pages[name] = page
        if name == "Search YouTube":
            page.pack(fill="x")
            self.toggle.pack(fill="x", padx=8, pady=(2, 4))
        return page

    def _toggle(self):
        self.set("Search YouTube" if self.current != "Search YouTube" else list(self.pages)[1])

    def set(self, name):
        self.current = name
        for key, page in self.pages.items():
            if key != "Search YouTube":
                page.pack_forget()
        expanded = name != "Search YouTube"
        if expanded:
            self.pages[name].pack(fill="x", pady=(0, 6))
        self.toggle.configure(text="−  Close bulk import" if expanded else
                              f"＋  {self.bulk_label}")

    def get(self):
        return self.current


class SmoothProgress(ctk.CTkProgressBar):
    """One short, interruptible transition; no idle animation timer."""

    def __init__(self, parent, *, reduced_motion, **kwargs):
        super().__init__(parent, **kwargs)
        self.reduced_motion = reduced_motion
        self._timer = None
        self._shown = 0.0
        super().set(0)

    def move_to(self, value):
        value = min(1.0, max(0.0, value))
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        if self.reduced_motion() or value <= self._shown:
            self._shown = value
            super().set(value)
            return
        start, began = self._shown, time.monotonic()

        def tick():
            fraction = min(1.0, (time.monotonic() - began) / 0.14)
            self._shown = start + (value - start) * (1 - (1 - fraction) ** 3)
            super(SmoothProgress, self).set(self._shown)
            self._timer = self.after(25, tick) if fraction < 1 else None

        tick()

    def destroy(self):
        if self._timer is not None:
            self.after_cancel(self._timer)
        super().destroy()
