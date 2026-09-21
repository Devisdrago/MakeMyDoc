"""Windows 11 (Fluent) look for the tkinter UI: colours, ttk theme, title bar, icons."""
from __future__ import annotations

import ctypes
import logging
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from tkinter import ttk
from typing import Optional

try:
    import sv_ttk
except Exception:  # pragma: no cover - falls back to the stock ttk theme
    sv_ttk = None

log = logging.getLogger(__name__)

LIGHT = {
    "bg": "#fafafa", "card": "#ffffff", "border": "#e0e0e0", "fg": "#1c1c1c", "muted": "#5f5f5f",
    "accent": "#005fb8", "accent_fg": "#ffffff", "danger": "#c42b1c", "ok": "#0f7b0f",
    "warn": "#9d5d00", "note_bg": "#e8f1fb", "warn_bg": "#fff4ce", "input_bg": "#ffffff",
    "thumb_bg": "#eeeeee", "canvas": "#2b2b2b", "caption": "#f3f3f3",
}
DARK = {
    "bg": "#1c1c1c", "card": "#2b2b2b", "border": "#404040", "fg": "#fafafa", "muted": "#a6a6a6",
    "accent": "#60cdff", "accent_fg": "#003a5c", "danger": "#ff99a4", "ok": "#6ccb5f",
    "warn": "#fce100", "note_bg": "#1f3244", "warn_bg": "#433519", "input_bg": "#2d2d2d",
    "thumb_bg": "#333333", "canvas": "#151515", "caption": "#202020",
}

pal: dict[str, str] = dict(LIGHT)
dark: bool = False
icon_font: Optional[str] = None

UI_FONT = "Segoe UI"


def system_is_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# Segoe Fluent Icons code points used in the UI.
GLYPH = {"up": "", "down": "", "delete": "", "edit": "", "undo": "",
         "add": "", "save": "", "export": "", "tag": ""}


def glyph(name: str, fallback: str) -> str:
    """Icon character when a Fluent/MDL2 icon font exists, else the plain-text fallback."""
    return GLYPH[name] if icon_font else fallback


def apply(root: tk.Tk, mode: str = "system") -> None:
    """Call once, right after creating the root window and before building any widget."""
    global dark, icon_font
    dark = system_is_dark() if mode == "system" else mode == "dark"
    pal.clear()
    pal.update(DARK if dark else LIGHT)

    if sv_ttk is not None:
        try:
            sv_ttk.set_theme("dark" if dark else "light", root)
        except TypeError:                        # older sv-ttk has no root argument
            sv_ttk.set_theme("dark" if dark else "light")
    root.configure(bg=pal["bg"])
    root.option_add("*Toplevel.background", pal["bg"])
    root.option_add("*Text.background", pal["input_bg"])
    # drop-down list of comboboxes (a plain tk Listbox)
    root.option_add("*TCombobox*Listbox.background", pal["card"])
    root.option_add("*TCombobox*Listbox.foreground", pal["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", pal["accent_fg"])
    root.option_add("*TCombobox*Listbox.font", (UI_FONT, 10))

    families = set(tkfont.families(root))
    icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)

    style = ttk.Style(root)
    bg, card, fg, muted = pal["bg"], pal["card"], pal["fg"], pal["muted"]
    style.configure("Title.TLabel", font=(UI_FONT, 20, "bold"))
    style.configure("Subtitle.TLabel", foreground=muted)
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("Heading.TLabel", font=(UI_FONT, 11, "bold"))
    style.configure("Ok.TLabel", foreground=pal["ok"])
    style.configure("Danger.TLabel", foreground=pal["danger"])
    style.configure("Warn.TLabel", foreground=pal["warn"], background=pal["warn_bg"])
    for name, colour in (("Card", card), ("Note", pal["note_bg"]), ("Warn", pal["warn_bg"])):
        style.configure(f"{name}.TFrame", background=colour)
        style.configure(f"{name}.TLabel", background=colour, foreground=fg)
        style.configure(f"{name}Muted.TLabel", background=colour, foreground=muted)
        style.configure(f"{name}.TCheckbutton", background=colour, foreground=fg)
    style.configure("Bar.TFrame", background=bg)
    if icon_font:
        style.configure("Icon.TButton", font=(icon_font, 11), padding=(8, 6))
    root.bind_class("Toplevel", "<Map>", _on_map, add="+")
    root.bind_class("Tk", "<Map>", _on_map, add="+")


# ---------------------------------------------------------------- title bar
_DWMWA_DARK = 20
_DWMWA_CORNER = 33
_DWMWA_BORDER = 34
_DWMWA_CAPTION = 35
_DWMWA_TEXT = 36


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def style_window(win: tk.Misc) -> None:
    """Dark/light title bar in the window colour and rounded corners (Windows 11)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        wid = win.winfo_id()
        hwnd = user32.GetParent(wid) or wid

        def put(attr: int, value: int) -> None:
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))

        put(_DWMWA_DARK, 1 if dark else 0)
        put(_DWMWA_CAPTION, _colorref(pal["caption"]))
        put(_DWMWA_TEXT, _colorref(pal["fg"]))
        put(_DWMWA_BORDER, _colorref(pal["border"]))
        put(_DWMWA_CORNER, 2)                    # DWMWCP_ROUND
    except Exception:
        log.debug("title bar styling unavailable", exc_info=True)


def _on_map(event) -> None:
    w = event.widget
    if not isinstance(w, (tk.Tk, tk.Toplevel)) or getattr(w, "_mmd_styled", False):
        return
    w._mmd_styled = True                         # type: ignore[attr-defined]
    style_window(w)


# ------------------------------------------------------------------ widgets
class TextInput(tk.Frame):
    """Multi-line text box with a Fluent-style 1px border that turns accent-coloured on focus."""

    def __init__(self, parent, height: int = 3, width: int = 40, font_size: int = 11):
        super().__init__(parent, bg=pal["border"], padx=1, pady=1)
        self.text = tk.Text(self, height=height, width=width, wrap="word", relief="flat", bd=0,
                            bg=pal["input_bg"], fg=pal["fg"], insertbackground=pal["fg"],
                            selectbackground=pal["accent"], selectforeground=pal["accent_fg"],
                            font=(UI_FONT, font_size), padx=10, pady=8, undo=False, highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<FocusIn>", lambda e: self.configure(bg=pal["accent"]), add="+")
        self.text.bind("<FocusOut>", lambda e: self.configure(bg=pal["border"]), add="+")


def status_dot(parent, size: int = 14, color: str = "#9ca3af", bg: Optional[str] = None):
    canvas = tk.Canvas(parent, width=size, height=size, highlightthickness=0, bg=bg or pal["card"])
    item = canvas.create_oval(2, 2, size - 2, size - 2, fill=color, outline="")
    return canvas, item


def menu(parent) -> tk.Menu:
    """Popup menu in the current palette (the native menu *bar* cannot be themed, popups can)."""
    return tk.Menu(parent, tearoff=False, bg=pal["card"], fg=pal["fg"], activebackground=pal["accent"],
                   activeforeground=pal["accent_fg"], selectcolor=pal["fg"], relief="flat", bd=1,
                   font=(UI_FONT, 10))
