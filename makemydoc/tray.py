"""System tray icon, always-on-top recording badge and a thread-safe bridge to Tk."""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from typing import Callable, Optional

from PIL import Image, ImageDraw

from . import winutil
from .capture import State

try:
    import pystray
except Exception:  # pragma: no cover - tray is optional
    pystray = None

log = logging.getLogger(__name__)

RED = "#ef4444"
AMBER = "#f59e0b"


class UiBridge:
    """Run callables on the Tk main thread from any other thread."""

    def __init__(self, root: tk.Misc):
        self._root = root
        self._q: "queue.Queue" = queue.Queue()
        root.after(50, self._pump)

    def post(self, fn: Callable, *args) -> None:
        self._q.put((fn, args))

    def _pump(self) -> None:
        try:
            while True:
                fn, args = self._q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        self._root.after(50, self._pump)


def make_icon(state: State, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, size - 4, size - 4], radius=13, fill=(31, 41, 55, 255))
    d.rectangle([18, 12, 42, 50], fill=(255, 255, 255, 255))            # page
    for y in (20, 27, 34):
        d.line([(23, y), (37, y)], fill=(156, 163, 175, 255), width=3)
    if state is State.RECORDING:
        d.ellipse([28, 28, 61, 61], fill=(239, 68, 68, 255), outline=(255, 255, 255, 255), width=3)
    elif state is State.PAUSED:
        d.ellipse([28, 28, 61, 61], fill=(245, 158, 11, 255), outline=(255, 255, 255, 255), width=3)
        d.rectangle([38, 37, 41, 52], fill=(255, 255, 255, 255))
        d.rectangle([47, 37, 50, 52], fill=(255, 255, 255, 255))
    return img


class Tray:
    """pystray wrapper. Callbacks run on pystray's thread: the owner must hop to Tk."""

    def __init__(self, actions: dict[str, Callable[[], None]]):
        self.available = pystray is not None
        self._actions = actions
        self._icon = None
        self._state = State.IDLE

    def start(self) -> None:
        if not self.available:
            return
        act = self._actions
        menu = pystray.Menu(
            pystray.MenuItem("Show MakeMyDoc", lambda: act["show"](), default=True),
            pystray.MenuItem("Start recording", lambda: act["start"](),
                             visible=lambda item: self._state is State.IDLE),
            pystray.MenuItem(lambda item: "Resume recording" if self._state is State.PAUSED else "Pause recording",
                             lambda: act["pause"](), visible=lambda item: self._state is not State.IDLE),
            pystray.MenuItem("Stop recording", lambda: act["stop"](),
                             visible=lambda item: self._state is not State.IDLE),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: act["quit"]()),
        )
        self._icon = pystray.Icon("MakeMyDoc", make_icon(State.IDLE), "MakeMyDoc", menu)
        try:
            self._icon.run_detached()
        except Exception:
            log.exception("Could not start the tray icon")
            self.available = False
            self._icon = None

    def set_state(self, state: State) -> None:
        self._state = state
        if self._icon is None:
            return
        titles = {State.IDLE: "MakeMyDoc", State.RECORDING: "MakeMyDoc - recording",
                  State.PAUSED: "MakeMyDoc - paused"}
        try:
            self._icon.icon = make_icon(state)
            self._icon.title = titles[state]
            self._icon.update_menu()
        except Exception:
            log.debug("tray update failed", exc_info=True)

    def notify(self, message: str, title: str = "MakeMyDoc") -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                log.debug("tray notify failed", exc_info=True)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


class Badge:
    """Small always-on-top 'REC' badge; excluded from screenshots."""

    def __init__(self, root: tk.Misc, on_pause: Optional[Callable[[], None]] = None,
                 on_stop: Optional[Callable[[], None]] = None):
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.92)
        frame = tk.Frame(self.win, bg="#111827", padx=8, pady=4)
        frame.pack()
        self.dot = tk.Canvas(frame, width=12, height=12, bg="#111827", highlightthickness=0)
        self.dot.pack(side="left")
        self.oval = self.dot.create_oval(1, 1, 11, 11, fill=RED, outline="")
        self.label = tk.Label(frame, text="REC", bg="#111827", fg="#f9fafb", font=("Segoe UI", 9, "bold"))
        self.label.pack(side="left", padx=(6, 8))
        # Clicks here belong to this process, so the recorder never turns them into steps.
        self.btn_pause = self._button(frame, "Pause", on_pause)
        self.btn_stop = self._button(frame, "Stop", on_stop)
        self._state = State.IDLE
        self._warning: Optional[str] = None
        self._blink_on = True
        self._job: Optional[str] = None
        self._excluded = False

    @staticmethod
    def _button(parent: tk.Misc, text: str, command: Optional[Callable[[], None]]) -> tk.Label:
        btn = tk.Label(parent, text=text, bg="#374151", fg="#f9fafb", font=("Segoe UI", 9),
                       padx=8, pady=1, cursor="hand2")
        btn.pack(side="left", padx=(0, 4))
        if command is not None:
            btn.bind("<Button-1>", lambda e: command())
        btn.bind("<Enter>", lambda e: btn.config(bg="#4b5563"))
        btn.bind("<Leave>", lambda e: btn.config(bg="#374151"))
        return btn

    def show(self, state: State) -> None:
        self._state = state
        self._refresh()
        self.win.deiconify()
        self.win.update_idletasks()
        if not self._excluded:
            winutil.exclude_from_capture(self.win)
            self._excluded = True
        self._place()
        if self._job is None:
            self._blink()

    def hide(self) -> None:
        self.win.withdraw()
        if self._job is not None:
            self.win.after_cancel(self._job)
            self._job = None

    def set_warning(self, message: Optional[str]) -> None:
        self._warning = message
        self._refresh()
        self._place()

    def _refresh(self) -> None:
        paused = self._state is State.PAUSED
        text = "PAUSED" if paused else "REC"
        if self._warning and not paused:
            text += "  - admin app not recorded"
        self.label.config(text=text, fg="#fde68a" if self._warning else "#f9fafb")
        self.btn_pause.config(text="Resume" if paused else "Pause")
        self.dot.itemconfigure(self.oval, fill=AMBER if paused or self._warning else RED)

    def _place(self) -> None:
        self.win.update_idletasks()
        x = self.win.winfo_screenwidth() - self.win.winfo_width() - 16
        self.win.geometry(f"+{max(0, x)}+12")

    def _blink(self) -> None:
        self._blink_on = not self._blink_on
        if self._state is State.RECORDING:
            color = AMBER if self._warning else RED
            self.dot.itemconfigure(self.oval, fill=color if self._blink_on else "#7f1d1d")
        self._job = self.win.after(700, self._blink)
