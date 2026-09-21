"""MakeMyDoc control window and application wiring."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import ImageTk

from . import __version__, theme, winutil
from .capture import Recorder, State
from .config import Config, config_dir, format_hotkey
from .review import ExportDialog, HotkeysDialog, LabelsDialog, ReviewWindow
from .session import Session
from .tray import RED, AMBER, Badge, Tray, UiBridge, make_icon

log = logging.getLogger("makemydoc")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        theme.apply(self, self.cfg.theme)
        self.session = Session()
        self.review: Optional[ReviewWindow] = None
        self.bridge = UiBridge(self)
        self._prev_state = State.IDLE
        self._warning: Optional[str] = None

        self.recorder = Recorder(
            self.cfg,
            on_state=lambda s: self.bridge.post(self._state_changed, s),
            on_step=lambda st: self.bridge.post(self._refresh_counts),
            on_warning=lambda m: self.bridge.post(self._warning_changed, m),
            on_hotkey=lambda a: self.bridge.post(self._hotkey, a),
        )
        self.tray = Tray({
            "show": lambda: self.bridge.post(self.show_window),
            "start": lambda: self.bridge.post(self.start_recording),
            "pause": lambda: self.bridge.post(self.recorder.toggle_pause),
            "stop": lambda: self.bridge.post(self.stop_recording),
            "quit": lambda: self.bridge.post(self.quit_app),
        })
        self.badge = Badge(self, on_pause=self.recorder.toggle_pause, on_stop=self.stop_recording)

        self.title("MakeMyDoc")
        self.resizable(False, False)
        self._icon = ImageTk.PhotoImage(make_icon(State.IDLE, 64))
        self.iconphoto(True, self._icon)      # also used by the review/editor windows
        self._build_menu()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.tray.start()
        self._state_changed(State.IDLE)
        self.update_idletasks()
        theme.style_window(self)

    # ------------------------------------------------------------------- ui
    def _build_menu(self) -> None:
        bar = ttk.Frame(self, padding=(12, 8, 12, 0))
        bar.pack(fill="x", side="top")

        def cascade(label: str, popup: tk.Menu) -> None:
            ttk.Menubutton(bar, text=label, menu=popup, style="Toolbutton").pack(side="left", padx=(0, 2))

        file_menu = theme.menu(bar)
        file_menu.add_command(label="New session", command=self.new_session)
        file_menu.add_command(label="Open session...", command=self.open_session)
        file_menu.add_command(label="Save session...", command=self.save_session)
        file_menu.add_separator()
        file_menu.add_command(label="Clean up temporary files...", command=self.cleanup)
        file_menu.add_separator()
        file_menu.add_command(label="Minimize to tray", command=self.hide_window)
        file_menu.add_command(label="Quit", command=self.quit_app)
        cascade("File", file_menu)

        tools = theme.menu(bar)
        tools.add_command(label="Label templates...", command=self.edit_labels)
        tools.add_command(label="Hotkeys...", command=self.edit_hotkeys)
        self.theme_var = tk.StringVar(value=self.cfg.theme)
        appearance = theme.menu(tools)
        for value, label in (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark")):
            appearance.add_radiobutton(label=label, value=value, variable=self.theme_var,
                                       command=self.change_theme)
        tools.add_cascade(label="Appearance", menu=appearance)
        if not winutil.is_self_elevated():
            tools.add_separator()
            tools.add_command(label="Restart as administrator...", command=self.restart_as_admin)
        cascade("Tools", tools)

        help_menu = theme.menu(bar)
        help_menu.add_command(label="About", command=self.about)
        cascade("Help", help_menu)

    def _build_ui(self) -> None:
        pal = theme.pal
        outer = ttk.Frame(self, padding=(24, 20, 24, 22))
        outer.pack(fill="both", expand=True)
        ttk.Frame(outer, width=360, height=1).pack()             # minimum width
        ttk.Label(outer, text="MakeMyDoc", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Turn clicks and typing into step-by-step guides",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(0, 16))

        card = ttk.Frame(outer, style="Card.TFrame", padding=(18, 16))
        card.pack(fill="x")
        head = ttk.Frame(card)
        head.pack(fill="x")
        self.dot, self.dot_item = theme.status_dot(head, 14, "#9ca3af", pal["bg"])
        self.dot.pack(side="left", padx=(0, 10))
        self.status_label = ttk.Label(head, text="Ready", font=(theme.UI_FONT, 12, "bold"))
        self.status_label.pack(side="left")
        self.steps_label = ttk.Label(head, text="0 steps", style="Muted.TLabel")
        self.steps_label.pack(side="right")

        row = ttk.Frame(card)
        row.pack(fill="x", pady=(16, 0))
        row.columnconfigure(0, weight=3, uniform="b")
        row.columnconfigure(1, weight=2, uniform="b")
        row.columnconfigure(2, weight=2, uniform="b")
        self.btn_start = ttk.Button(row, text="Start", style="Accent.TButton", command=self.start_recording)
        self.btn_pause = ttk.Button(row, text="Pause", command=self.recorder.toggle_pause)
        self.btn_stop = ttk.Button(row, text="Stop", command=self.stop_recording)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_pause.grid(row=0, column=1, sticky="ew", padx=3)
        self.btn_stop.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=(12, 0))
        row2.columnconfigure(0, weight=1, uniform="c")
        row2.columnconfigure(1, weight=1, uniform="c")
        self.btn_review = ttk.Button(row2, text="Review / Edit...", command=self.open_review)
        self.btn_export = ttk.Button(row2, text="Export...", command=self.export)
        self.btn_review.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_export.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.warn_frame = ttk.Frame(outer, style="Warn.TFrame", padding=(12, 10))
        self.warn_label = tk.Label(self.warn_frame, text="", bg=pal["warn_bg"], fg=pal["fg"], wraplength=320,
                                   justify="left", anchor="w", font=(theme.UI_FONT, 10))
        self.warn_label.pack(fill="x")

        self.hint_label = ttk.Label(outer, style="Muted.TLabel", justify="left")
        self.hint_label.pack(fill="x", pady=(16, 0))
        self._update_hint()

    def _update_hint(self) -> None:
        self.hint_label.config(
            text=f"Pause / resume    {format_hotkey(self.cfg.hotkey_pause)}\n"
                 f"Stop recording    {format_hotkey(self.cfg.hotkey_stop)}")

    # ----------------------------------------------------------- window state
    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_window(self) -> None:
        if self.tray.available:
            self.withdraw()
        else:
            self.iconify()

    def _on_unmap(self, event) -> None:
        if event.widget is self and self.state() == "iconic" and self.tray.available:
            self.after(50, self.withdraw)

    def _on_close(self) -> None:
        if self.recorder.state is not State.IDLE:
            self.hide_window()
        else:
            self.quit_app()

    # ---------------------------------------------------------------- recording
    def start_recording(self) -> None:
        if self.recorder.state is not State.IDLE:
            return
        if self.review is not None and self.review.winfo_exists():
            self.review.close()
        if self.session.steps:
            self.show_window()              # the dialog needs a visible parent (tray start)
            n = self.session.numbered_count()
            answer = messagebox.askyesnocancel(
                "Start recording",
                f"The current session already has {n} step(s).\n\n"
                "Yes - add the new steps to it\nNo - discard it and start a new session\nCancel - go back",
                parent=self)
            if answer is None:
                return
            if answer is False:
                if not self._confirm_discard():
                    return
                self._reset_session()
        try:
            self.recorder.start(self.session)
        except ValueError as exc:
            messagebox.showerror("Cannot start", f"Invalid hotkey setting:\n{exc}", parent=self)
            return
        self.hide_window()

    def stop_recording(self) -> None:
        if self.recorder.state is not State.IDLE:
            self.recorder.stop()

    def _hotkey(self, action: str) -> None:
        if action == "pause":
            self.recorder.toggle_pause()
        elif action == "stop":
            self.stop_recording()

    def _state_changed(self, state: State) -> None:
        prev, self._prev_state = self._prev_state, state
        idle, rec = state is State.IDLE, state is State.RECORDING
        color = {State.IDLE: "#9ca3af", State.RECORDING: RED, State.PAUSED: AMBER}[state]
        text = {State.IDLE: "Ready", State.RECORDING: "Recording...", State.PAUSED: "Paused"}[state]
        self.dot.itemconfigure(self.dot_item, fill=color)
        self.status_label.config(text=text)
        self.btn_start.config(state="normal" if idle else "disabled")
        self.btn_pause.config(state="disabled" if idle else "normal", text="Resume" if state is State.PAUSED else "Pause")
        self.btn_stop.config(state="disabled" if idle else "normal")
        self.btn_review.config(state="normal" if idle else "disabled")
        self.btn_export.config(state="normal" if idle else "disabled")
        self.tray.set_state(state)
        if idle:
            self.badge.hide()
            self._warning_changed(None)
        elif self.cfg.show_badge:
            self.badge.show(state)
        self._refresh_counts()
        if idle and prev is not State.IDLE:
            self.show_window()
            if self.session.steps:
                self.open_review()

    def _refresh_counts(self) -> None:
        n = self.session.numbered_count()
        self.steps_label.config(text=f"{n} step{'s' if n != 1 else ''}")

    def _warning_changed(self, message: Optional[str]) -> None:
        if message == self._warning:
            return
        self._warning = message
        self.warn_label.config(text=message or "")
        if message:
            self.warn_frame.pack(fill="x", pady=(12, 0), before=self.hint_label)
        else:
            self.warn_frame.pack_forget()
        self.badge.set_warning(message)
        if message:
            self.tray.notify(message, "MakeMyDoc cannot record this window")

    # ------------------------------------------------------------------ review
    def open_review(self) -> None:
        if not self.session.steps:
            messagebox.showinfo("Review", "There is nothing to review yet. Press Start to record.", parent=self)
            return
        if self.review is not None and self.review.winfo_exists():
            self.review.deiconify()
            self.review.lift()
            return
        self.review = ReviewWindow(self, self.session, self.cfg, on_change=self._refresh_counts)

    def export(self) -> None:
        if not self.session.steps:
            messagebox.showinfo("Export", "There is nothing to export yet.", parent=self)
            return
        if self.review is not None and self.review.winfo_exists():
            self.review.commit_all()
        self.show_window()
        ExportDialog(self, self.session, self.cfg)

    # ---------------------------------------------------------------- sessions
    def _confirm_discard(self) -> bool:
        if not (self.session.steps and self.session.dirty):
            return True
        return messagebox.askokcancel(
            "Discard session", "The current session has unsaved changes and will be discarded.", parent=self)

    def _reset_session(self) -> None:
        if self.review is not None and self.review.winfo_exists():
            self.review.close()
        self.session.cleanup()
        self.session = Session()
        self._refresh_counts()

    def new_session(self) -> None:
        if self.recorder.state is not State.IDLE:
            return
        if self._confirm_discard():
            self._reset_session()

    def open_session(self) -> None:
        if self.recorder.state is not State.IDLE or not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("MakeMyDoc session", "*.json")],
            initialdir=self.cfg.last_session_dir or None)
        if not path:
            return
        try:
            loaded = Session.load(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Open session", f"Could not open this session:\n{exc}", parent=self)
            return
        self._reset_session()
        self.session = loaded
        self.cfg.last_session_dir = os.path.dirname(path)
        self.cfg.save()
        self._refresh_counts()
        self.open_review()

    def save_session(self) -> bool:
        if not self.session.steps:
            messagebox.showinfo("Save session", "There is nothing to save yet.", parent=self)
            return False
        if self.review is not None and self.review.winfo_exists():
            self.review.commit_all()
        from .export import safe_filename
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json", filetypes=[("MakeMyDoc session", "*.json")],
            initialdir=self.cfg.last_session_dir or None, initialfile=safe_filename(self.session.title))
        if not path:
            return False
        try:
            self.session.save(path)
        except OSError as exc:
            messagebox.showerror("Save session", f"Could not save the session:\n{exc}", parent=self)
            return False
        self.cfg.last_session_dir = os.path.dirname(path)
        self.cfg.save()
        return True

    def cleanup(self) -> None:
        if self.recorder.state is not State.IDLE:
            return
        if not messagebox.askokcancel(
                "Clean up temporary files",
                "This deletes the screenshots of the current session and of any older sessions left "
                "in the temp folder. Saved session (.json) and exported files are not touched.\n\n"
                "The current session will be emptied. Continue?", parent=self):
            return
        self._reset_session()
        removed = Session.cleanup_all_temp()
        messagebox.showinfo("Clean up", f"Removed {removed} leftover session folder(s).", parent=self)

    # ---------------------------------------------------------------- settings
    def edit_labels(self) -> None:
        LabelsDialog(self, self.cfg, lambda: self.review.rebuild()
                     if self.review is not None and self.review.winfo_exists() else None)

    def edit_hotkeys(self) -> None:
        HotkeysDialog(self, self.cfg, self._update_hint)

    def change_theme(self) -> None:
        self.cfg.theme = self.theme_var.get()
        self.cfg.save()
        messagebox.showinfo("Appearance", "Restart MakeMyDoc to apply the new appearance.", parent=self)

    def restart_as_admin(self) -> None:
        if self.recorder.state is not State.IDLE:
            messagebox.showinfo("Restart", "Stop recording first.", parent=self)
            return
        if not self._confirm_save():
            return
        if winutil.relaunch_as_admin():
            self._shutdown()
        else:
            messagebox.showwarning("Restart", "The elevated restart was cancelled.", parent=self)

    def about(self) -> None:
        messagebox.showinfo(
            "About MakeMyDoc",
            f"MakeMyDoc {__version__}\nRecord clicks and keystrokes into step-by-step documentation.\n\n"
            "MIT License", parent=self)

    # -------------------------------------------------------------------- quit
    def _confirm_save(self) -> bool:
        if not (self.session.steps and self.session.dirty):
            return True
        answer = messagebox.askyesnocancel("MakeMyDoc", "Save the current session first?", parent=self)
        if answer is None:
            return False
        return self.save_session() if answer else True

    def quit_app(self) -> None:
        if self.recorder.state is not State.IDLE:
            self.recorder.stop()
        if not self._confirm_save():
            return
        self._shutdown()

    def _shutdown(self) -> None:
        self.tray.stop()
        self.destroy()


def _setup_logging() -> None:
    if sys.stdout is None:                  # windowed PyInstaller build: libraries may print
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        os.makedirs(config_dir(), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(config_dir(), "makemydoc.log"), maxBytes=200_000, backupCount=1, encoding="utf-8")
        logging.basicConfig(level=logging.WARNING, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    except OSError:
        logging.basicConfig(level=logging.WARNING)


def main() -> None:
    _setup_logging()
    winutil.enable_dpi_awareness()          # before any window exists
    App().mainloop()


if __name__ == "__main__":
    main()
