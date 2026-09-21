"""Tkinter review/edit screen, image editor and the dialogs around it."""
from __future__ import annotations

import copy
import math
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from . import annotate, export, theme
from .config import (DEFAULT_LABELS, LABEL_CAPTIONS, LABEL_PLACEHOLDERS, Config,
                     format_hotkey, parse_hotkey)
from .session import Annotation, Session, Step, auto_text, step_text

FONT = (theme.UI_FONT, 10)


def _center_on(win: tk.Toplevel, parent: tk.Misc) -> None:
    win.update_idletasks()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_width()) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height() - win.winfo_height()) // 3)
    win.geometry(f"+{x}+{y}")


def _make_modal(win: tk.Toplevel) -> None:
    try:
        win.wait_visibility()
        win.grab_set()
    except tk.TclError:
        pass                                # not viewable yet: dialog still works, just not modal


# --------------------------------------------------------------- small dialogs
class TextPrompt(tk.Toplevel):
    """Modal multi-line text prompt. `result` is None when cancelled."""

    def __init__(self, parent, title: str, prompt: str, initial: str = ""):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Optional[str] = None
        ttk.Label(self, text=prompt).pack(anchor="w", padx=20, pady=(18, 8))
        box = theme.TextInput(self, height=5, width=52, font_size=10)
        box.pack(padx=20)
        self.text = box.text
        self.text.insert("1.0", initial)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=20, pady=18)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="OK", style="Accent.TButton", command=self._ok).pack(side="right", padx=8)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Control-Return>", lambda e: self._ok())
        _center_on(self, parent)
        self.text.focus_set()
        self.wait_visibility()
        self.grab_set()
        self.wait_window()

    def _ok(self) -> None:
        self.result = self.text.get("1.0", "end-1c")
        self.destroy()


def ask_text(parent, title: str, prompt: str, initial: str = "") -> Optional[str]:
    return TextPrompt(parent, title, prompt, initial).result


class LabelsDialog(tk.Toplevel):
    """Edit the step text templates once; they apply to every step."""

    def __init__(self, parent, cfg: Config, on_apply: Callable[[], None]):
        super().__init__(parent)
        self.title("Label templates")
        self.transient(parent)
        self.resizable(False, False)
        self.cfg, self.on_apply = cfg, on_apply
        self.vars: dict[str, tk.StringVar] = {}
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, wraplength=520, justify="left",
                  text="These templates generate the description of every step that you have not "
                       "edited by hand. Changing one updates all matching steps.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        for i, key in enumerate(DEFAULT_LABELS, start=1):
            ttk.Label(body, text=LABEL_CAPTIONS[key]).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            var = tk.StringVar(value=cfg.labels.get(key, DEFAULT_LABELS[key]))
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=42).grid(row=i, column=1, sticky="ew", pady=4)
        hint = "Placeholders: {name} (element name), {type} (element type), {x} {y} (screen position), {text} (typed text)"
        ttk.Label(body, text=hint, style="Muted.TLabel").grid(
            row=len(DEFAULT_LABELS) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        bar = ttk.Frame(body)
        bar.grid(row=len(DEFAULT_LABELS) + 2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(bar, text="Restore defaults", command=self._defaults).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Apply", style="Accent.TButton", command=self._apply).pack(side="right", padx=8)
        _center_on(self, parent)
        _make_modal(self)

    def _defaults(self) -> None:
        for key, var in self.vars.items():
            var.set(DEFAULT_LABELS[key])

    def _apply(self) -> None:
        for key, var in self.vars.items():
            value = var.get()
            self.cfg.labels[key] = value if value.strip() else DEFAULT_LABELS[key]
        self.cfg.save()
        self.on_apply()
        self.destroy()


class HotkeysDialog(tk.Toplevel):
    def __init__(self, parent, cfg: Config, on_apply: Callable[[], None]):
        super().__init__(parent)
        self.title("Hotkeys")
        self.transient(parent)
        self.resizable(False, False)
        self.cfg, self.on_apply = cfg, on_apply
        body = ttk.Frame(self, padding=20)
        body.pack()
        self.pause_var = tk.StringVar(value=cfg.hotkey_pause)
        self.stop_var = tk.StringVar(value=cfg.hotkey_stop)
        ttk.Label(body, text="Pause / resume").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.pause_var, width=22).grid(row=0, column=1, padx=8)
        ttk.Label(body, text="Stop recording").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.stop_var, width=22).grid(row=1, column=1, padx=8)
        ttk.Label(body, style="Muted.TLabel", text="Format: ctrl+shift+p  (modifiers: ctrl, alt, shift, win)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        bar = ttk.Frame(body)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=8)
        _center_on(self, parent)
        _make_modal(self)

    def _save(self) -> None:
        pause, stop = self.pause_var.get().strip().lower(), self.stop_var.get().strip().lower()
        try:
            a, b = parse_hotkey(pause), parse_hotkey(stop)
        except ValueError as exc:
            messagebox.showerror("Invalid hotkey", str(exc), parent=self)
            return
        if a == b:
            messagebox.showerror("Invalid hotkey", "Pause and stop need different hotkeys.", parent=self)
            return
        self.cfg.hotkey_pause, self.cfg.hotkey_stop = pause, stop
        self.cfg.save()
        self.on_apply()
        self.destroy()


# ------------------------------------------------------------ thumbnail loader
class ThumbLoader:
    """Renders step thumbnails on a background thread; Tk objects stay on the UI thread."""

    def __init__(self, session: Session, cfg: Config, width: int, max_height: int):
        self.session, self.cfg, self.width, self.max_height = session, cfg, width, max_height
        self.cache: dict[str, ImageTk.PhotoImage] = {}
        self._pending: set[str] = set()
        self._jobs: "queue.Queue" = queue.Queue()
        self._results: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._run, name="mmd-thumbs", daemon=True).start()

    def get(self, key: str) -> Optional[ImageTk.PhotoImage]:
        return self.cache.get(key)

    def request(self, step: Step, text: str, key: str) -> None:
        if key in self.cache or key in self._pending:
            return
        path = self.session.image_path(step)
        if not path:
            return
        self._pending.add(key)
        self._jobs.put((key, path, copy.deepcopy(step), text))

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            key, path, step, text = job
            try:
                img = annotate.render_step_file(path, step, text, self.cfg)
                self._results.put((key, annotate.thumbnail(img, self.width, self.max_height)))
            except Exception:
                self._results.put((key, None))

    def poll(self) -> list[str]:
        """Called on the UI thread: wrap finished thumbnails and return their keys."""
        done = []
        while True:
            try:
                key, img = self._results.get_nowait()
            except queue.Empty:
                return done
            self._pending.discard(key)
            if img is not None:
                self.cache[key] = ImageTk.PhotoImage(img)
                done.append(key)

    def stop(self) -> None:
        self._jobs.put(None)


# ----------------------------------------------------------------- image editor
def _dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _sig(anns: list[Annotation]) -> tuple:
    return tuple((a.id, a.kind, round(a.x0, 1), round(a.y0, 1), round(a.x1, 1), round(a.y1, 1), a.text)
                 for a in anns)


class ImageEditor(tk.Toplevel):
    """Move/resize the auto highlight, draw new shapes, blur, arrows, warnings, callouts."""

    TOOLS = (("select", "Select / move"), ("highlight", "Highlight"), ("circle", "Circle"),
             ("arrow", "Arrow"), ("blur", "Blur"), ("warning", "Warning"), ("callout", "Callout"),
             ("crop", "Crop"))
    RECT_KINDS = ("highlight", "circle", "blur", "warning", "callout", "crop")

    def __init__(self, master, session: Session, step: Step, cfg: Config,
                 on_apply: Callable[[list[Annotation], bool, Optional[list[float]]], None]):
        super().__init__(master)
        self.title("Edit image")
        self.transient(master)
        self.cfg, self.on_apply = cfg, on_apply
        with Image.open(session.image_path(step)) as im:
            self.raw = im.convert("RGB")
        self.anns: list[Annotation] = copy.deepcopy(step.annotations)
        box = annotate.crop_box(step.crop, self.raw.size)
        if box:                             # the crop is edited like any other shape
            self.anns.append(Annotation("crop", *box))
        self.show_inset = tk.BooleanVar(value=step.show_inset)
        self.tool = tk.StringVar(value="select")
        self.selected: Optional[Annotation] = None
        self._undo: list[list[Annotation]] = []
        self._drag: Optional[dict] = None
        self._photo = None

        w, h = self.raw.size
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.scale = min(1.0, (sw - 160) / w, (sh - 260) / h)
        self.dw, self.dh = max(1, int(w * self.scale)), max(1, int(h * self.scale))
        self._build()
        self.refresh_bg()
        self.redraw()
        self._sync_buttons()
        self.wait_visibility()
        self.grab_set()

    # ---- ui
    def _build(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10))
        bar.pack(fill="x")
        for value, label in self.TOOLS:
            ttk.Radiobutton(bar, text=label, value=value, variable=self.tool, style="Toggle.TButton",
                            command=self._tool_changed).pack(side="left", padx=1)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_text = ttk.Button(bar, text="Edit text...", command=self.edit_text, state="disabled")
        self.btn_text.pack(side="left", padx=2)
        self.btn_delete = ttk.Button(bar, text="Delete shape", command=self.delete_selected, state="disabled")
        self.btn_delete.pack(side="left", padx=2)
        self.btn_reset_crop = ttk.Button(bar, text="Reset crop", command=self.reset_crop, state="disabled")
        self.btn_reset_crop.pack(side="left", padx=2)
        ttk.Button(bar, text="Undo", command=self.undo).pack(side="left", padx=2)
        ttk.Checkbutton(bar, text="Zoom inset", variable=self.show_inset).pack(side="left", padx=12)

        self.canvas = tk.Canvas(self, width=self.dw, height=self.dh, highlightthickness=0, bg=theme.pal["canvas"])
        self.canvas.pack(padx=12)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.edit_text())

        foot = ttk.Frame(self, padding=(12, 12))
        foot.pack(fill="x")
        ttk.Label(foot, style="Muted.TLabel", wraplength=max(300, self.dw - 240),
                  text="Select: click a shape, drag to move, drag a square handle to resize. "
                       "Other tools: drag on the image to draw. Delete removes the selected shape.").pack(
            side="left", fill="x", expand=True)
        ttk.Button(foot, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="Apply", style="Accent.TButton", command=self.apply).pack(side="right", padx=8)
        self.bind("<Delete>", lambda e: self.delete_selected())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Escape>", lambda e: self._deselect())

    def _tool_changed(self) -> None:
        if self.tool.get() != "select":
            self.selected = None
            self.redraw()
            self._sync_buttons()

    def _deselect(self) -> None:
        self.selected = None
        self.redraw()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        sel = self.selected
        has_crop = any(a.kind == "crop" for a in self.anns)
        self.btn_reset_crop.config(state="normal" if has_crop else "disabled")
        self.btn_delete.config(state="normal" if sel else "disabled")
        self.btn_text.config(state="normal" if sel and sel.kind in ("warning", "callout") else "disabled")

    # ---- drawing
    def refresh_bg(self) -> None:
        img = annotate.render_blurred(self.raw, self.anns, self.cfg.blur_strength)
        if self.scale != 1.0:
            img = img.resize((self.dw, self.dh), Image.Resampling.BILINEAR)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw", tags="bg")
        self.canvas.tag_lower("bg")

    def redraw(self) -> None:
        self.canvas.delete("ann", "handle")
        for a in self.anns:
            self._draw(a)
        if self.selected is not None:
            s, hs = self.scale, 5
            for x, y in self._handles(self.selected).values():
                self.canvas.create_rectangle(x * s - hs, y * s - hs, x * s + hs, y * s + hs,
                                             fill="#ffffff", outline="#2563eb", width=2, tags="handle")

    def _draw(self, a: Annotation) -> None:
        c, s = self.canvas, self.scale
        x0, y0, x1, y1 = (v * s for v in a.box())
        tag = ("ann", a.id)
        red = self.cfg.highlight_color
        if a.kind == "highlight":
            c.create_rectangle(x0, y0, x1, y1, outline=red, width=3, tags=tag)
        elif a.kind == "circle":
            c.create_oval(x0, y0, x1, y1, outline=red, width=3, tags=tag)
        elif a.kind == "arrow":
            c.create_line(a.x0 * s, a.y0 * s, a.x1 * s, a.y1 * s, fill=red, width=3,
                          arrow="last", arrowshape=(16, 20, 6), tags=tag)
        elif a.kind == "blur":
            c.create_rectangle(x0, y0, x1, y1, outline="#2563eb", width=2, dash=(5, 3), tags=tag)
            c.create_text(x0 + 4, y0 + 2, anchor="nw", text="blur", fill="#2563eb", font=("Segoe UI", 8), tags=tag)
        elif a.kind == "warning":
            c.create_rectangle(x0, y0, x1, y1, outline="#f59e0b", width=3, dash=(6, 4), tags=tag)
            c.create_text(x0 + 2, y0 - 2, anchor="sw", text="⚠", fill="#d97706",
                          font=("Segoe UI Symbol", 16, "bold"), tags=tag)
            if a.text.strip():
                c.create_text(x0, y1 + 4, anchor="nw", text=a.text, fill="#78350f",
                              width=max(180, x1 - x0), font=("Segoe UI", 9), tags=tag)
        elif a.kind == "crop":
            for r in ((0, 0, self.dw, y0), (0, y1, self.dw, self.dh), (0, y0, x0, y1), (x1, y0, self.dw, y1)):
                c.create_rectangle(*r, fill="#000000", stipple="gray50", outline="", tags=tag)
            c.create_rectangle(x0, y0, x1, y1, outline="#ffffff", width=2, tags=tag)
            c.create_rectangle(x0, y0, x1, y1, outline="#2563eb", width=2, dash=(6, 4), tags=tag)
            pw, ph = int(abs(a.x1 - a.x0)), int(abs(a.y1 - a.y0))
            c.create_text(x0 + 6, y0 + 4, anchor="nw", text=f"crop {pw} x {ph}", fill="#ffffff",
                          font=("Segoe UI", 9, "bold"), tags=tag)
        elif a.kind == "callout":
            c.create_rectangle(x0, y0, x1, y1, fill="#fff8c5", outline="#d4a72c", width=2, tags=tag)
            c.create_text(x0 + 6, y0 + 6, anchor="nw", text=a.text, fill="#24292f",
                          width=max(40, x1 - x0 - 12), font=("Segoe UI", 9), tags=tag)

    # ---- geometry
    def _pt(self, e) -> tuple[float, float]:
        w, h = self.raw.size
        return (min(max(e.x / self.scale, 0), w), min(max(e.y / self.scale, 0), h))

    @staticmethod
    def _handles(a: Annotation) -> dict[str, tuple[float, float]]:
        if a.kind == "arrow":
            return {"tail": (a.x0, a.y0), "head": (a.x1, a.y1)}
        l, t, r, b = a.box()
        return {"nw": (l, t), "ne": (r, t), "sw": (l, b), "se": (r, b)}

    def _handle_at(self, a: Annotation, ix: float, iy: float) -> Optional[str]:
        tol = 9 / self.scale
        for name, (x, y) in self._handles(a).items():
            if abs(ix - x) <= tol and abs(iy - y) <= tol:
                return name
        return None

    def _hit(self, ix: float, iy: float) -> Optional[Annotation]:
        tol = 8 / self.scale
        for a in reversed(self.anns):
            if a.kind == "arrow":
                if _dist_to_segment(ix, iy, a.x0, a.y0, a.x1, a.y1) <= tol:
                    return a
            else:
                l, t, r, b = a.box()
                if not (l - tol <= ix <= r + tol and t - tol <= iy <= b + tol):
                    continue
                if a.kind == "crop" and l + tol < ix < r - tol and t + tol < iy < b - tol:
                    continue                # only the border grabs a crop, so shapes inside stay reachable
                return a
        return None

    # ---- mouse
    def _press(self, e) -> None:
        ix, iy = self._pt(e)
        snap = copy.deepcopy(self.anns)
        if self.tool.get() == "select":
            sel = self.selected
            if sel is not None:
                handle = self._handle_at(sel, ix, iy)
                if handle:
                    if sel.kind != "arrow":
                        sel.x0, sel.y0, sel.x1, sel.y1 = sel.box()
                    self._drag = {"mode": "resize", "ann": sel, "handle": handle, "snap": snap}
                    return
            self.selected = self._hit(ix, iy)
            if self.selected is not None:
                a = self.selected
                self._drag = {"mode": "move", "ann": a, "sx": ix, "sy": iy,
                              "orig": (a.x0, a.y0, a.x1, a.y1), "snap": snap}
            self.redraw()
            self._sync_buttons()
        else:
            a = Annotation(kind=self.tool.get(), x0=ix, y0=iy, x1=ix, y1=iy)
            if a.kind == "crop":            # one crop per image
                self.anns = [x for x in self.anns if x.kind != "crop"]
            self.anns.append(a)
            self.selected = a
            self._drag = {"mode": "create", "ann": a, "snap": snap}
            self.redraw()

    def _motion(self, e) -> None:
        d = self._drag
        if not d:
            return
        a, (ix, iy) = d["ann"], self._pt(e)
        w, h = self.raw.size
        if d["mode"] == "create":
            a.x1, a.y1 = ix, iy
        elif d["mode"] == "move":
            ox0, oy0, ox1, oy1 = d["orig"]
            dx = max(-min(ox0, ox1), min(ix - d["sx"], w - max(ox0, ox1)))
            dy = max(-min(oy0, oy1), min(iy - d["sy"], h - max(oy0, oy1)))
            a.x0, a.y0, a.x1, a.y1 = ox0 + dx, oy0 + dy, ox1 + dx, oy1 + dy
        else:
            handle = d["handle"]
            if handle == "tail":
                a.x0, a.y0 = ix, iy
            elif handle == "head":
                a.x1, a.y1 = ix, iy
            else:
                if "w" in handle:
                    a.x0 = ix
                else:
                    a.x1 = ix
                if "n" in handle:
                    a.y0 = iy
                else:
                    a.y1 = iy
        self.redraw()

    def _release(self, e) -> None:
        d, self._drag = self._drag, None
        if not d:
            return
        a, mode = d["ann"], d["mode"]
        w, h = self.raw.size
        if a.kind != "arrow":
            a.x0, a.y0, a.x1, a.y1 = a.box()
        committed = True
        if mode == "create":
            if a.kind == "arrow":
                too_small = math.hypot(a.x1 - a.x0, a.y1 - a.y0) < 10 / self.scale
            else:
                too_small = (a.x1 - a.x0) < 6 / self.scale or (a.y1 - a.y0) < 6 / self.scale
            if too_small and a.kind in ("warning", "callout"):
                bw, bh = 240, (90 if a.kind == "callout" else 60)
                a.x0 = max(0, min(a.x0, w - bw))
                a.y0 = max(0, min(a.y0, h - bh))
                a.x1, a.y1 = a.x0 + bw, a.y0 + bh
            elif too_small:
                if a.kind == "crop":
                    self.anns = d["snap"]   # a stray click must not wipe the existing crop
                else:
                    self.anns.remove(a)
                self.selected = None
                committed = False
            if committed and a.kind in ("warning", "callout"):
                label = "Warning note" if a.kind == "warning" else "Callout"
                text = ask_text(self, label, "Text shown next to this area:", "")
                if text is None:
                    self.anns.remove(a)
                    self.selected = None
                    committed = False
                else:
                    a.text = text.strip()
            if committed:
                self.tool.set("select")
        elif _sig(self.anns) == _sig(d["snap"]):
            committed = False
        if committed:
            self._undo.append(d["snap"])
        if any(x.kind == "blur" for x in self.anns) or any(x.kind == "blur" for x in d["snap"]):
            self.refresh_bg()
        self.redraw()
        self._sync_buttons()

    # ---- actions
    def _push(self) -> None:
        self._undo.append(copy.deepcopy(self.anns))

    def edit_text(self) -> None:
        a = self.selected
        if a is None or a.kind not in ("warning", "callout"):
            return
        text = ask_text(self, "Edit text", "Text shown next to this area:", a.text)
        if text is not None and text.strip() != a.text:
            self._push()
            a.text = text.strip()
            self.redraw()

    def reset_crop(self) -> None:
        if not any(a.kind == "crop" for a in self.anns):
            return
        self._push()
        self.anns = [a for a in self.anns if a.kind != "crop"]
        if self.selected is not None and self.selected.kind == "crop":
            self.selected = None
        self.redraw()
        self._sync_buttons()

    def delete_selected(self) -> None:
        a = self.selected
        if a is None:
            return
        self._push()
        self.anns.remove(a)
        self.selected = None
        self.refresh_bg()
        self.redraw()
        self._sync_buttons()

    def undo(self) -> None:
        if not self._undo:
            return
        self.anns = self._undo.pop()
        self.selected = None
        self.refresh_bg()
        self.redraw()
        self._sync_buttons()

    def apply(self) -> None:
        crop_ann = next((a for a in self.anns if a.kind == "crop"), None)
        box = annotate.crop_box(list(crop_ann.box()), self.raw.size) if crop_ann else None
        shapes = [a for a in self.anns if a.kind != "crop"]
        self.on_apply(shapes, self.show_inset.get(), [float(v) for v in box] if box else None)
        self.destroy()


# ---------------------------------------------------------------- export dialog
class ExportDialog(tk.Toplevel):
    def __init__(self, parent, session: Session, cfg: Config):
        super().__init__(parent)
        self.title("Export")
        self.transient(parent)
        self.resizable(False, False)
        self.session, self.cfg = session, cfg
        self._q: "queue.Queue" = queue.Queue()
        self._running = False
        self._folder = ""

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Document title").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar(value=session.title)
        ttk.Entry(body, textvariable=self.title_var, width=46).grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Label(body, text="Formats").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.fmt_vars: dict[str, tk.BooleanVar] = {}
        box = ttk.Frame(body)
        box.grid(row=1, column=1, columnspan=2, sticky="w", pady=(8, 0))
        for key, name in export.FORMAT_NAMES.items():
            var = tk.BooleanVar(value=True)
            self.fmt_vars[key] = var
            ttk.Checkbutton(box, text=name, variable=var).pack(side="left", padx=(0, 14))
        self.below_var = tk.BooleanVar(value=cfg.text_below_image)
        ttk.Checkbutton(body, text="Also print the description below each screenshot",
                        variable=self.below_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(body, text="Save to folder").grid(row=2, column=0, sticky="w", pady=(8, 0))
        default = cfg.last_export_dir or os.path.join(os.path.expanduser("~"), "Documents")
        self.dir_var = tk.StringVar(value=default)
        ttk.Entry(body, textvariable=self.dir_var, width=38).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(body, text="Browse...", command=self._browse).grid(row=2, column=2, padx=(6, 0), pady=(8, 0))
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 2))
        self.status = ttk.Label(body, text="", style="Muted.TLabel")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w")
        bar = ttk.Frame(body)
        bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.btn_close = ttk.Button(bar, text="Close", command=self._close)
        self.btn_close.pack(side="right")
        self.btn_export = ttk.Button(bar, text="Export", style="Accent.TButton", command=self._start)
        self.btn_export.pack(side="right", padx=8)
        self.btn_open = ttk.Button(bar, text="Open folder", command=self._open_folder, state="disabled")
        self.btn_open.pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._close)
        _center_on(self, parent)
        _make_modal(self)

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=self.dir_var.get() or None)
        if path:
            self.dir_var.set(path)

    def _close(self) -> None:
        if not self._running:
            self.destroy()

    def _open_folder(self) -> None:
        if self._folder and os.path.isdir(self._folder):
            os.startfile(self._folder)  # noqa: S606 - Windows-only app

    def _start(self) -> None:
        formats = [k for k, v in self.fmt_vars.items() if v.get()]
        title = self.title_var.get().strip() or "Untitled guide"
        folder = self.dir_var.get().strip()
        if not formats:
            messagebox.showwarning("Export", "Choose at least one format.", parent=self)
            return
        if not folder:
            messagebox.showwarning("Export", "Choose a folder to save to.", parent=self)
            return
        if not self.session.steps:
            messagebox.showwarning("Export", "There are no steps to export.", parent=self)
            return
        self.session.title = title
        self.session.dirty = True
        self.cfg.last_export_dir = folder
        self.cfg.text_below_image = self.below_var.get()
        self.cfg.save()
        self._folder = folder
        self._running = True
        self.btn_export.config(state="disabled")
        self.btn_close.config(state="disabled")
        steps = copy.deepcopy(self.session.steps)

        def work() -> None:
            try:
                paths = export.export_all(
                    self.session, self.cfg, formats, folder, title, steps=steps,
                    progress=lambda d, t, m: self._q.put(("progress", d, t, m)))
                self._q.put(("done", paths))
            except Exception as exc:  # shown to the user
                self._q.put(("error", str(exc)))

        threading.Thread(target=work, name="mmd-export", daemon=True).start()
        self.after(80, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                if msg[0] == "progress":
                    _, done, total, text = msg
                    self.progress.config(value=100 * done / max(1, total))
                    self.status.config(text=text)
                elif msg[0] == "done":
                    self._finish(f"Exported {len(msg[1])} file(s) to {self._folder}", ok=True)
                    return
                else:
                    self._finish(f"Export failed: {msg[1]}", ok=False)
                    return
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _finish(self, text: str, ok: bool) -> None:
        self._running = False
        self.status.config(text=text, style="Ok.TLabel" if ok else "Danger.TLabel")
        self.progress.config(value=100 if ok else 0)
        self.btn_export.config(state="normal")
        self.btn_close.config(state="normal")
        if ok:
            self.btn_open.config(state="normal")


# ------------------------------------------------------------ add note dialog
class AddNoteDialog(tk.Toplevel):
    """Add a note or warning at any position. Stays open so several can be added in a row."""

    def __init__(self, review: "ReviewWindow", kind: str = "note"):
        super().__init__(review)
        self.review = review
        self.title("Add note or warning")
        self.transient(review)
        self.resizable(False, False)
        self.kind_var = tk.StringVar(value=kind)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Add").grid(row=0, column=0, sticky="w", pady=(0, 12))
        kinds = ttk.Frame(body)
        kinds.grid(row=0, column=1, sticky="w", pady=(0, 12))
        for value, label in (("note", "Note"), ("warning", "Warning")):
            ttk.Radiobutton(kinds, text=label, value=value, variable=self.kind_var,
                            style="Toggle.TButton").pack(side="left", padx=(0, 6))
        ttk.Label(body, text="Place it").grid(row=1, column=0, sticky="w", padx=(0, 14))
        self.combo = ttk.Combobox(body, state="readonly", width=58)
        self.combo.grid(row=1, column=1, sticky="ew")
        ttk.Label(body, style="Muted.TLabel", text="Then type the text in the new box in the list. "
                  "Add as many as you need.").grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        bar = ttk.Frame(body)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Add", style="Accent.TButton", command=self._add).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self._add())
        self.bind("<Escape>", lambda e: self.destroy())
        self.refresh()
        _center_on(self, review)

    def refresh(self, select: Optional[int] = None) -> None:
        labels = self.review.position_labels()
        self.combo.configure(values=labels)
        self.combo.current(len(labels) - 1 if select is None or select < 0 else min(select, len(labels) - 1))

    def _add(self) -> None:
        index = self.combo.current()
        self.review.add_note(index, self.kind_var.get())
        self.refresh(select=index + 1)      # the next one goes right after the one just added


# ---------------------------------------------------------------- review window
class ReviewWindow(tk.Toplevel):
    """Scrollable list of steps: thumbnail + editable description, reorder, delete, undo."""

    THUMB_W = 600
    THUMB_MAX_H = 400
    NOTE_BG = {"note": "#eef4ff", "warning": "#fff7d6"}

    def __init__(self, master, session: Session, cfg: Config, on_change: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.session, self.cfg = session, cfg
        self.on_change = on_change or (lambda: None)
        self.title("MakeMyDoc - Review and edit")
        self.geometry("1060x760")
        self.minsize(780, 480)
        self.loader = ThumbLoader(session, cfg, self.THUMB_W, self.THUMB_MAX_H)
        self._text_widgets: dict[str, tuple[Step, tk.Text]] = {}
        self._thumbs: dict[str, tuple[tk.Label, str]] = {}
        self._focus_step: Optional[str] = None
        self._closed = False

        self._build_toolbar()
        self._build_list()
        self.status = ttk.Label(self, anchor="w", padding=(16, 8), style="Muted.TLabel")
        self.status.pack(fill="x", side="bottom")
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<MouseWheel>", self._on_wheel)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.rebuild()
        self.after(100, self._poll_thumbs)

    # ---- layout
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(16, 12))
        bar.pack(fill="x")
        ttk.Label(bar, text="Title", style="Heading.TLabel").pack(side="left")
        self.title_var = tk.StringVar(value=self.session.title)
        entry = ttk.Entry(bar, textvariable=self.title_var, width=28)
        entry.pack(side="left", padx=(8, 16))
        entry.bind("<KeyRelease>", lambda e: self._title_changed())
        ttk.Button(bar, text="Undo", command=self.undo).pack(side="left", padx=2)
        ttk.Button(bar, text="Labels...", command=self.edit_labels).pack(side="left", padx=2)
        ttk.Button(bar, text="+ Note", command=lambda: self.open_add_dialog("note")).pack(
            side="left", padx=(16, 2))
        ttk.Button(bar, text="+ Warning", command=lambda: self.open_add_dialog("warning")).pack(
            side="left", padx=2)
        ttk.Button(bar, text="Export...", style="Accent.TButton", command=self.export).pack(side="right", padx=2)
        ttk.Button(bar, text="Save session...", command=self.save_session).pack(side="right", padx=6)

    def _build_list(self) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg=theme.pal["bg"])
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=theme.pal["bg"])
        self.inner.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))

    def _on_wheel(self, e) -> None:
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # ---- rows
    def rebuild(self) -> None:
        top = self.canvas.yview()[0]
        for child in self.inner.winfo_children():
            child.destroy()
        self._text_widgets.clear()
        self._thumbs.clear()
        numbers = self.session.numbers()
        steps = self.session.steps
        if not steps:
            tk.Label(self.inner, bg=theme.pal["bg"], fg=theme.pal["muted"], font=(theme.UI_FONT, 11), pady=40,
                     text="No steps yet. Record something, or use + Note to add text.").grid(row=0, column=0)
        focus_row = None
        for i, step in enumerate(steps):
            row = self._build_row(i, step, numbers.get(step.id))
            if step.id == self._focus_step:
                focus_row = row
        focus_widget = self._text_widgets[self._focus_step][1] if focus_row is not None else None
        self._focus_step = None
        self._update_status()
        dlg = getattr(self, '_add_dialog', None)
        if dlg is not None and dlg.winfo_exists():
            dlg.refresh(dlg.combo.current())
        self.update_idletasks()
        if focus_row is not None:
            total = max(1, self.inner.winfo_height())
            self.canvas.yview_moveto(max(0.0, (focus_row.winfo_y() - 20) / total))
            focus_widget.focus_set()
        else:
            self.canvas.yview_moveto(top)

    def _build_row(self, index: int, step: Step, number: Optional[int]) -> tk.Frame:
        pal = theme.pal
        is_note = step.kind == "note"
        if is_note:
            bg = pal["warn_bg"] if step.note_level == "warning" else pal["note_bg"]
            row = tk.Frame(self.inner, bg=bg, padx=14, pady=12)
        else:
            bg = pal["bg"]                  # Card.TFrame is filled with the window colour
            row = ttk.Frame(self.inner, style="Card.TFrame", padding=(14, 12))
        row.grid(row=index, column=0, sticky="ew", padx=16, pady=6)
        row.step = step  # type: ignore[attr-defined]

        if number:
            tk.Label(row, text=str(number), bg=bg, fg=pal["accent"], font=(theme.UI_FONT, 18, "bold"),
                     width=3, anchor="n").grid(row=0, column=0, sticky="n", padx=(0, 6))
        else:
            caption = "WARNING" if step.note_level == "warning" else "NOTE"
            tk.Label(row, text=caption, bg=bg, fg=pal["warn"] if step.note_level == "warning" else pal["accent"],
                     font=(theme.UI_FONT, 8, "bold"), width=8, anchor="n").grid(row=0, column=0, sticky="n")

        has_image = bool(step.image and self.session.image_path(step))
        right = tk.Frame(row, bg=bg)                 # picture on top, description and buttons below it
        # With a picture the whole block is centred in the card; without one (notes) it fills the width.
        right.grid(row=0, column=1, sticky="n" if has_image else "nsew", padx=(8, 0))
        # Column 0 is as wide as the picture (the description box below it matches).
        right.columnconfigure(0, weight=0 if has_image else 1, minsize=320)
        row.columnconfigure(1, weight=1)

        if has_image:
            text = step_text(step, self.cfg.labels)
            key = annotate.render_key(step, text, self.cfg)
            photo = self.loader.get(key)
            thumb = tk.Label(right, bg=pal["thumb_bg"], fg=pal["muted"], cursor="hand2", text="Rendering...",
                             width=70, height=14)
            if photo:
                thumb.config(image=photo, text="", width=0, height=0)
            else:
                self.loader.request(step, text, key)
            thumb.grid(row=0, column=0, sticky="w", pady=(0, 12))
            thumb.bind("<Button-1>", lambda e, s=step: self.open_editor(s))
            self._thumbs[step.id] = (thumb, key)
        elif not is_note:
            tk.Label(right, bg=bg, fg=pal["muted"], text="(no screenshot)", anchor="w").grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        if is_note:
            initial = step.custom_text or ""
            hint = "Warning text" if step.note_level == "warning" else "Note text"
        else:
            initial = step_text(step, self.cfg.labels)
            hint = "Description"
        tk.Label(right, text=hint, bg=bg, fg=pal["muted"], font=(theme.UI_FONT, 9)).grid(
            row=1, column=0, sticky="w", pady=(0, 4))
        box = theme.TextInput(right, height=3, width=10)       # width comes from the picture's column
        box.grid(row=2, column=0, columnspan=1 if has_image else 2, sticky="ew")
        text_box = box.text
        text_box.insert("1.0", initial)
        text_box.bind("<FocusOut>", lambda e, s=step, w=text_box: self._commit_text(s, w), add="+")
        text_box.bind("<Control-Return>", lambda e, s=step, w=text_box: (self._commit_text(s, w), "break")[1])
        self._text_widgets[step.id] = (step, text_box)

        btns = tk.Frame(right, bg=bg)
        btns.grid(row=3, column=0, columnspan=2, sticky="" if has_image else "w", pady=(10, 0))
        last = len(self.session.steps) - 1
        icon = "Icon.TButton" if theme.icon_font else "TButton"
        up = ttk.Button(btns, text=theme.glyph("up", "\u25b2"), style=icon, width=3,
                        command=lambda i=index: self.move(i, -1))
        down = ttk.Button(btns, text=theme.glyph("down", "\u25bc"), style=icon, width=3,
                          command=lambda i=index: self.move(i, 1))
        up.pack(side="left", padx=(0, 4))
        down.pack(side="left", padx=(0, 12))
        if index == 0:
            up.state(["disabled"])
        if index == last:
            down.state(["disabled"])
        if step.image and self.session.image_path(step):
            ttk.Button(btns, text="Edit image...", command=lambda s=step: self.open_editor(s)).pack(side="left", padx=2)
            var = tk.BooleanVar(value=step.show_inset)
            ttk.Checkbutton(btns, text="Zoom inset", variable=var,
                            command=lambda s=step, v=var: self.set_inset(s, v.get())).pack(side="left", padx=10)
        if is_note:
            other = "warning" if step.note_level == "note" else "note"
            ttk.Button(btns, text=f"Make {other}", command=lambda s=step, o=other: self.set_level(s, o)).pack(
                side="left", padx=2)
        elif step.custom_text is not None:
            ttk.Button(btns, text="Reset text", command=lambda s=step: self.reset_text(s)).pack(side="left", padx=2)
        menu_btn = ttk.Menubutton(btns, text="Insert below")
        menu = theme.menu(menu_btn)
        menu.add_command(label="Note", command=lambda i=index: self.add_note(i + 1, "note"))
        menu.add_command(label="Warning", command=lambda i=index: self.add_note(i + 1, "warning"))
        menu_btn["menu"] = menu
        menu_btn.pack(side="left", padx=2)
        ttk.Button(btns, text=theme.glyph("delete", "Delete"), style=icon, width=3 if theme.icon_font else 8,
                   command=lambda i=index: self.delete(i)).pack(side="left", padx=(12, 0))
        return row

    def _update_status(self) -> None:
        n, total = self.session.numbered_count(), len(self.session.steps)
        self.status.config(text=f"{n} step(s), {total - n} note(s)   |   Undo: Ctrl+Z   |   "
                                f"Click a thumbnail to edit the image")

    def _poll_thumbs(self) -> None:
        if self._closed:
            return
        for key in self.loader.poll():
            photo = self.loader.get(key)
            for label, k in self._thumbs.values():
                if k == key and label.winfo_exists():
                    label.config(image=photo, text="", width=0, height=0)
        self.after(100, self._poll_thumbs)

    # ---- editing
    def _touch(self) -> None:
        self.session.dirty = True
        self.on_change()

    def _commit_text(self, step: Step, widget: tk.Text) -> None:
        try:
            new = widget.get("1.0", "end-1c")
        except tk.TclError:
            return                          # row already destroyed
        if step.kind == "note":
            if new == (step.custom_text or ""):
                return
            self.session.push_undo()
            step.custom_text = new
        else:
            if new == step_text(step, self.cfg.labels):
                return
            self.session.push_undo()
            step.custom_text = None if new == auto_text(step, self.cfg.labels) else new
        self._touch()
        self._refresh_thumb(step)

    def commit_all(self) -> None:
        for step, widget in list(self._text_widgets.values()):
            self._commit_text(step, widget)

    def _refresh_thumb(self, step: Step) -> None:
        entry = self._thumbs.get(step.id)
        if not entry:
            return
        label, _ = entry
        text = step_text(step, self.cfg.labels)
        key = annotate.render_key(step, text, self.cfg)
        self._thumbs[step.id] = (label, key)
        photo = self.loader.get(key)
        if photo:
            label.config(image=photo, text="", width=0, height=0)
        else:
            self.loader.request(step, text, key)

    def _title_changed(self) -> None:
        self.session.title = self.title_var.get()
        self.session.dirty = True

    def move(self, index: int, delta: int) -> None:
        self.commit_all()
        j = index + delta
        if not 0 <= j < len(self.session.steps):
            return
        self.session.push_undo()
        steps = self.session.steps
        steps[index], steps[j] = steps[j], steps[index]
        self._touch()
        self.rebuild()

    def delete(self, index: int) -> None:
        self.commit_all()
        self.session.push_undo()
        del self.session.steps[index]
        self._touch()
        self.rebuild()

    def position_labels(self) -> list[str]:
        """Choices for where a new note goes; entry k means 'insert at index k'."""
        def short(text: str) -> str:
            text = " ".join((text or "").split())
            return text if len(text) <= 46 else text[:45] + "..."

        numbers = self.session.numbers()
        labels = ["At the beginning"]
        for step in self.session.steps:
            if step.kind == "note":
                kind = "warning" if step.note_level == "warning" else "note"
                labels.append(f"After {kind}: {short(step.custom_text or '')}".rstrip(": "))
            else:
                labels.append(f"After step {numbers[step.id]}: {short(step_text(step, self.cfg.labels))}")
        if len(labels) > 1:
            labels[-1] += "  (at the end)"
        return labels

    def open_add_dialog(self, kind: str = "note") -> None:
        self.commit_all()
        existing = getattr(self, "_add_dialog", None)
        if existing is not None and existing.winfo_exists():
            existing.kind_var.set(kind)
            existing.refresh()
            existing.lift()
            return
        self._add_dialog = AddNoteDialog(self, kind)

    def add_note(self, index: int, level: str) -> None:
        self.commit_all()
        self.session.push_undo()
        note = Step(kind="note", note_level=level, custom_text="")
        self.session.steps.insert(index, note)
        self._focus_step = note.id
        self._touch()
        self.rebuild()

    def set_level(self, step: Step, level: str) -> None:
        self.commit_all()
        self.session.push_undo()
        step.note_level = level
        self._touch()
        self.rebuild()

    def reset_text(self, step: Step) -> None:
        self.commit_all()
        self.session.push_undo()
        step.custom_text = None
        self._touch()
        self.rebuild()

    def set_inset(self, step: Step, value: bool) -> None:
        self.commit_all()
        self.session.push_undo()
        step.show_inset = value
        self._touch()
        self._refresh_thumb(step)

    def undo(self) -> None:
        self.commit_all()
        if self.session.undo():
            self._touch()
            self.rebuild()
        else:
            self.status.config(text="Nothing to undo.")

    def edit_labels(self) -> None:
        self.commit_all()
        LabelsDialog(self, self.cfg, self._labels_applied)

    def _labels_applied(self) -> None:
        self._touch()
        self.rebuild()

    def open_editor(self, step: Step) -> None:
        self.commit_all()
        if not self.session.image_path(step):
            return
        ImageEditor(self, self.session, step, self.cfg,
                    lambda anns, inset, crop, s=step: self._editor_applied(s, anns, inset, crop))

    def _editor_applied(self, step: Step, anns: list[Annotation], inset: bool,
                        crop: Optional[list[float]]) -> None:
        self.session.push_undo()
        step.annotations = anns
        step.show_inset = inset
        step.crop = crop
        self._touch()
        self.rebuild()

    # ---- session / export
    def save_session(self) -> None:
        self.commit_all()
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json", filetypes=[("MakeMyDoc session", "*.json")],
            initialdir=self.cfg.last_session_dir or None, initialfile=export.safe_filename(self.session.title))
        if not path:
            return
        try:
            self.session.save(path)
        except OSError as exc:
            messagebox.showerror("Save session", f"Could not save the session:\n{exc}", parent=self)
            return
        self.cfg.last_session_dir = os.path.dirname(path)
        self.cfg.save()
        self.status.config(text=f"Session saved to {path}")
        self.on_change()

    def export(self) -> None:
        self.commit_all()
        ExportDialog(self, self.session, self.cfg)

    def close(self) -> None:
        self.commit_all()
        self._closed = True
        self.loader.stop()
        self.on_change()
        self.destroy()
