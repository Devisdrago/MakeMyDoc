"""Global mouse/keyboard hooks, active-window screenshots and UI Automation lookups.

Threading model
---------------
* pynput listener threads only do cheap work: filter events, take the screenshot
  (it must show the screen *at click time*) and push an event onto a queue.
* One worker thread consumes the queue in order. It owns all UI Automation (COM)
  calls, sensitive-data blurring, file writes and session mutation.
"""
from __future__ import annotations

import contextlib
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import mss
from PIL import Image
from pynput import keyboard, mouse

from . import annotate, winutil
from .config import Config, parse_hotkey
from .session import Annotation, Session, Step

try:  # UI Automation is optional at runtime: without it we fall back to coordinates.
    import uiautomation as auto
except Exception:  # pragma: no cover - depends on the machine
    auto = None


def _uia_client():
    """The raw IUIAutomation COM object (not re-exported by the package's __init__)."""
    module = getattr(auto, "uiautomation", None)
    cls = getattr(module, "_AutomationClient", None) or getattr(auto, "_AutomationClient", None)
    if cls is None:
        raise RuntimeError("uiautomation: no access to the IUIAutomation client")
    return cls.instance().IUIAutomation

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"


# ------------------------------------------------------------------ screenshots
_tls = threading.local()


def _sct() -> mss.base.MSSBase:
    """Per-thread mss instance, refreshed periodically so monitor changes are seen."""
    inst = getattr(_tls, "sct", None)
    if inst is None or time.monotonic() - _tls.created > 5.0:
        if inst is not None:
            with contextlib.suppress(Exception):
                inst.close()
        inst = (getattr(mss, "MSS", None) or mss.mss)()   # mss.mss is deprecated in newer releases
        _tls.sct = inst
        _tls.created = time.monotonic()
    return inst


def virtual_screen() -> Rect:
    m = _sct().monitors[0]
    return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])


def grab_region(rect: Rect) -> tuple[Image.Image, tuple[int, int]]:
    """Grab a screen rectangle (virtual-desktop coordinates, works across monitors)."""
    vl, vt, vr, vb = virtual_screen()
    l, t = max(rect[0], vl), max(rect[1], vt)
    r, b = min(rect[2], vr), min(rect[3], vb)
    if r - l < 2 or b - t < 2:
        raise ValueError("empty capture region")
    shot = _sct().grab({"left": l, "top": t, "width": r - l, "height": b - t})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), (l, t)


@dataclass
class Shot:
    img: Image.Image
    left: int
    top: int
    title: str = ""
    hwnd: Optional[int] = None
    fullscreen: bool = False
    scanned: bool = False               # password fields already blurred
    filename: Optional[str] = None      # set once written to the session folder


def _union(a: Rect, b: Rect) -> Rect:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _contains(rect: Rect, x: int, y: int) -> bool:
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


def _monitor_at(x: int, y: int) -> Rect:
    for m in _sct().monitors[1:]:
        r = (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
        if _contains(r, x, y):
            return r
    return virtual_screen()


def capture_desktop(point: Optional[tuple[int, int]] = None) -> Shot:
    """Fallback: the whole monitor under the cursor (or the whole virtual desktop)."""
    rect = _monitor_at(*point) if point else virtual_screen()
    img, (left, top) = grab_region(rect)
    hwnd = winutil.foreground_window()
    return Shot(img, left, top, title="Desktop", hwnd=hwnd, fullscreen=True)


def capture_active(point: Optional[tuple[int, int]] = None) -> Optional[Shot]:
    """Screenshot of the active window only; falls back to the full desktop.

    `point` is where the user clicked. Returns None when the foreground window is
    MakeMyDoc itself and no point was given.
    """
    try:
        fg = winutil.foreground_window()
        fg_rect = winutil.window_rect(fg) if fg else None
        if point is None:
            if not fg or not fg_rect or winutil.window_pid(fg) == winutil.OWN_PID:
                return None
            point = ((fg_rect[0] + fg_rect[2]) // 2, (fg_rect[1] + fg_rect[3]) // 2)

        chosen, rect = None, None
        if fg and fg_rect and winutil.is_capturable(fg) and _contains(fg_rect, *point):
            chosen, rect = fg, fg_rect
            # Menus / drop-downs are separate top-level windows of the same app and
            # may stick out of the main window: include them.
            root = winutil.root_window_at(*point)
            if root and root != fg and winutil.window_pid(root) == winutil.window_pid(fg):
                r2 = winutil.window_rect(root)
                if r2 and winutil.is_capturable(root):
                    rect = _union(rect, r2)
        else:
            root = winutil.root_window_at(*point)
            if root and winutil.window_pid(root) != winutil.OWN_PID and winutil.is_capturable(root):
                chosen, rect = root, winutil.window_rect(root)

        if chosen is not None and rect and rect[2] - rect[0] >= 40 and rect[3] - rect[1] >= 24:
            img, (left, top) = grab_region(rect)
            return Shot(img, left, top, title=winutil.window_title(chosen), hwnd=chosen)
    except Exception:
        log.exception("Active-window capture failed; falling back to the desktop")
    try:
        return capture_desktop(point)
    except Exception:
        log.exception("Desktop capture failed")
        return None


# ------------------------------------------------------------------ UI Automation
@dataclass
class ElementInfo:
    name: str = ""
    ctype: str = ""
    rect: Optional[Rect] = None         # screen coordinates
    is_password: bool = False


_NAMEABLE_PARENTS = {
    "ButtonControl", "ListItemControl", "MenuItemControl", "TabItemControl",
    "TreeItemControl", "DataItemControl", "HyperlinkControl", "CheckBoxControl",
    "RadioButtonControl", "ComboBoxControl", "SplitButtonControl", "EditControl",
    "HeaderItemControl",
}
_JUNK = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")


def _clean(text: object) -> str:
    s = _JUNK.sub("", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:100]


def _ctype(ctrl) -> str:
    name = getattr(ctrl, "ControlTypeName", "") or ""
    return name[:-7] if name.endswith("Control") else name


def _rect_of(ctrl) -> Optional[Rect]:
    r = ctrl.BoundingRectangle
    if r.right - r.left > 0 and r.bottom - r.top > 0:
        return (r.left, r.top, r.right, r.bottom)
    return None


def _area(r: Rect) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


# ------------------------------------------------------------------ keyboard
_MOD_KEYS: dict = {}
for _n, _m in (("ctrl", "ctrl"), ("ctrl_l", "ctrl"), ("ctrl_r", "ctrl"),
               ("shift", "shift"), ("shift_l", "shift"), ("shift_r", "shift"),
               ("alt", "alt"), ("alt_l", "alt"), ("alt_r", "alt"), ("alt_gr", "altgr"),
               ("cmd", "win"), ("cmd_l", "win"), ("cmd_r", "win")):
    _k = getattr(keyboard.Key, _n, None)
    if _k is not None:
        _MOD_KEYS[_k] = _m

_SPECIAL = {"enter": "Enter", "tab": "Tab", "esc": "Esc", "delete": "Delete"}
_SPECIAL.update({f"f{i}": f"F{i}" for i in range(1, 13)})
_MOD_ORDER = ("ctrl", "alt", "shift", "win")


def _key_name(key) -> Optional[str]:
    """Layout-independent lower-case name of a key ('p', '5', 'enter', 'f5')."""
    if isinstance(key, keyboard.Key):
        return key.name
    vk = getattr(key, "vk", None)
    if vk is not None:
        if 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x30 <= vk <= 0x39:
            return chr(vk)
    ch = getattr(key, "char", None)
    return ch.lower() if ch else None


@dataclass
class _KeyEvent:
    kind: str       # char | special | combo | bs
    value: str
    t: float


@dataclass
class _ClickEvent:
    x: int
    y: int
    button: str
    shot: Shot


@dataclass
class _DoubleEvent:
    x: int
    y: int


_STOP = object()
_FLUSH = object()


@dataclass
class _TypeBuffer:
    tokens: list[str] = field(default_factory=list)
    secure: bool = False
    last_t: float = 0.0


class Recorder:
    """Records clicks and keystrokes of the whole desktop into a Session."""

    SETTLE_SECONDS = 0.6        # after this much typing silence, screenshot the field

    def __init__(self, cfg: Config,
                 on_state: Optional[Callable[[State], None]] = None,
                 on_step: Optional[Callable[[Step], None]] = None,
                 on_warning: Optional[Callable[[Optional[str]], None]] = None,
                 on_hotkey: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.session: Optional[Session] = None
        self._on_state = on_state or (lambda s: None)
        self._on_step = on_step or (lambda s: None)
        self._on_warning = on_warning or (lambda m: None)
        self._on_hotkey = on_hotkey or (lambda a: None)

        self._state = State.IDLE
        self._q: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._mouse: Optional[mouse.Listener] = None
        self._kb: Optional[keyboard.Listener] = None
        self._held: set[str] = set()
        self._hotkeys: list[tuple[frozenset[str], str, str]] = []

        # mouse-thread state
        self._last_click: Optional[tuple[float, int, int]] = None
        # worker-thread state
        self._buf = _TypeBuffer()
        self._settled: Optional[Shot] = None
        self._last_step: Optional[Step] = None
        self._focus_cache = (0.0, False)
        self._last_secure_rect: Optional[Rect] = None
        self._scan_ok = True
        self._scan_warned = False
        self._blocked: Optional[str] = None
        self._last_elev_check = 0.0

    # -------------------------------------------------------------- control
    @property
    def state(self) -> State:
        return self._state

    def start(self, session: Session) -> None:
        if self._state is not State.IDLE:
            return
        self.session = session
        self._hotkeys = []
        for spec, action in ((self.cfg.hotkey_pause, "pause"), (self.cfg.hotkey_stop, "stop")):
            mods, key = parse_hotkey(spec)
            self._hotkeys.append((mods, key, action))
        self._q = queue.Queue()
        self._buf = _TypeBuffer()
        self._settled = None
        self._last_step = None
        self._last_click = None
        self._held.clear()
        self._focus_cache = (0.0, False)
        self._last_secure_rect = None
        self._scan_ok = True
        self._blocked = None
        self._last_elev_check = 0.0

        self._worker = threading.Thread(target=self._run, name="mmd-worker", daemon=True)
        self._worker.start()
        self._mouse = mouse.Listener(on_click=self._on_click)
        self._kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._mouse.daemon = self._kb.daemon = True
        self._mouse.start()
        self._kb.start()
        self._set_state(State.RECORDING)

    def pause(self) -> None:
        if self._state is State.RECORDING:
            self._q.put(_FLUSH)         # close the current typing group
            self._set_state(State.PAUSED)

    def resume(self) -> None:
        if self._state is State.PAUSED:
            self._set_state(State.RECORDING)

    def toggle_pause(self) -> None:
        if self._state is State.RECORDING:
            self.pause()
        elif self._state is State.PAUSED:
            self.resume()

    def stop(self) -> None:
        if self._state is State.IDLE:
            return
        self._state = State.IDLE            # listeners stop accepting events
        for lst in (self._mouse, self._kb):
            if lst is not None:
                with contextlib.suppress(Exception):
                    lst.stop()
        self._q.put(_STOP)
        if self._worker is not None:
            self._worker.join(timeout=8)
        self._mouse = self._kb = self._worker = None
        self._on_warning(None)
        self._on_state(State.IDLE)

    def _set_state(self, state: State) -> None:
        self._state = state
        self._on_state(state)

    # ------------------------------------------------- listener threads (fast)
    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed or self._state is not State.RECORDING:
            return
        if button not in (mouse.Button.left, mouse.Button.right):
            return
        try:
            if winutil.window_pid_at(x, y) == winutil.OWN_PID:
                self._last_click = None        # clicks on MakeMyDoc itself are never recorded
                return
            now = time.monotonic()
            if button == mouse.Button.left:
                last = self._last_click
                dx, dy = winutil.double_click_slop()
                if last and now - last[0] <= winutil.double_click_time() \
                        and abs(x - last[1]) <= dx and abs(y - last[2]) <= dy:
                    self._last_click = None
                    self._q.put(_DoubleEvent(x, y))
                    return
                self._last_click = (now, x, y)
            else:
                self._last_click = None
            shot = capture_active((x, y))
            if shot is not None:
                self._q.put(_ClickEvent(x, y, "left" if button == mouse.Button.left else "right", shot))
        except Exception:
            log.exception("click handler failed")

    def _active_mods(self) -> frozenset[str]:
        held = set(self._held)
        if "altgr" in held:                 # AltGr is reported as Ctrl+Alt but types text
            held -= {"ctrl", "alt", "altgr"}
        return frozenset(held & set(_MOD_ORDER))

    def _on_press(self, key) -> None:
        try:
            mod = _MOD_KEYS.get(key)
            if mod:
                self._held.add(mod)
                return
            name = _key_name(key)
            active = self._active_mods()
            if name:
                for mods, hk_key, action in self._hotkeys:
                    if name == hk_key and active == mods:
                        self._on_hotkey(action)
                        return
            if self._state is not State.RECORDING or winutil.foreground_is_own():
                return
            ev = self._key_event(key, name, active)
            if ev is not None:
                self._q.put(ev)
        except Exception:
            log.exception("key handler failed")

    def _on_release(self, key) -> None:
        mod = _MOD_KEYS.get(key)
        if mod:
            self._held.discard(mod)

    @staticmethod
    def _key_event(key, name: Optional[str], active: frozenset[str]) -> Optional[_KeyEvent]:
        now = time.monotonic()
        if isinstance(key, keyboard.Key):
            if key == keyboard.Key.space:
                label, char = "Space", " "
            elif key == keyboard.Key.backspace:
                label, char = "Backspace", None
            elif key.name in _SPECIAL:
                label, char = _SPECIAL[key.name], None
            else:
                return None                 # arrows, caps lock, ... are noise
        else:
            ch = getattr(key, "char", None)
            char = ch if ch and ch.isprintable() else None
            if name is None and char is None:
                return None
            label = (name or char).upper()
        chord = active & {"ctrl", "alt", "win"}
        if chord or (active == {"shift"} and char is None):
            parts = [m.capitalize() for m in _MOD_ORDER if m in active]
            return _KeyEvent("combo", "[" + "+".join(parts + [label]) + "]", now)
        if label == "Backspace":
            return _KeyEvent("bs", "", now)
        if char is not None:
            return _KeyEvent("char", char, now)
        return _KeyEvent("special", f"[{label}]", now)

    # ------------------------------------------------------------ worker thread
    def _run(self) -> None:
        ctx = auto.UIAutomationInitializerInThread() if auto is not None else contextlib.nullcontext()
        try:
            with ctx:
                while True:
                    try:
                        ev = self._q.get(timeout=0.25)
                    except queue.Empty:
                        self._tick()
                        continue
                    if ev is _STOP:
                        break
                    try:
                        self._dispatch(ev)
                    except Exception:
                        log.exception("event failed")
                try:
                    self._flush_typing()
                except Exception:
                    log.exception("final flush failed")
        except Exception:
            log.exception("worker crashed")

    def _dispatch(self, ev) -> None:
        if ev is _FLUSH:
            self._flush_typing()
        elif isinstance(ev, _KeyEvent):
            self._handle_key(ev)
        elif isinstance(ev, _ClickEvent):
            self._handle_click(ev)
        elif isinstance(ev, _DoubleEvent):
            self._handle_double()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._state is State.RECORDING and now - self._last_elev_check > 1.0:
            self._last_elev_check = now
            blocked = winutil.foreground_blocked()
            if blocked != self._blocked:
                self._blocked = blocked
                self._on_warning(
                    f"'{blocked}' runs as administrator - restart MakeMyDoc as administrator to record it."
                    if blocked else None)
        buf = self._buf
        if buf.tokens and self._settled is None and now - buf.last_t > self.SETTLE_SECONDS \
                and not winutil.foreground_is_own():
            self._settled = capture_active()

    # --- keyboard grouping
    def _focus_secure(self) -> bool:
        now = time.monotonic()
        if now - self._focus_cache[0] < 0.15:
            return self._focus_cache[1]
        secure = False
        if auto is not None:
            try:
                ctrl = auto.GetFocusedControl()
                if ctrl is not None and ctrl.IsPassword:
                    secure = True
                    self._last_secure_rect = _rect_of(ctrl)
            except Exception:
                secure = False
        self._focus_cache = (now, secure)
        return secure

    def _handle_key(self, ev: _KeyEvent) -> None:
        secure = self._focus_secure()
        buf = self._buf
        if buf.tokens and buf.secure != secure:
            self._flush_typing()
            buf = self._buf
        buf.secure = secure
        buf.last_t = ev.t
        self._settled = None
        if secure:
            if not buf.tokens:
                buf.tokens.append("*")      # remembers that something was typed, never what
            return
        if ev.kind == "bs":
            if buf.tokens and len(buf.tokens[-1]) == 1:
                buf.tokens.pop()
            else:
                buf.tokens.append("[Backspace]")
        else:
            buf.tokens.append(ev.value)

    def _flush_typing(self, shot: Optional[Shot] = None) -> None:
        """Turn the buffered keystrokes into one 'type' step."""
        buf = self._buf
        if not buf.tokens:
            return
        self._buf = _TypeBuffer()
        secure = buf.secure
        text = "" if secure else "".join(buf.tokens)
        if shot is None:
            shot = self._settled
            if shot is None and not winutil.foreground_is_own():
                shot = capture_active()
        self._settled = None
        step = Step(kind="type", typed=text, secret=secure)
        if shot is not None:
            extra = [self._last_secure_rect] if secure and self._last_secure_rect else []
            self._prepare_shot(shot, extra)
            step.image = self._store(shot)
            step.window_title = shot.title
            step.fullscreen = shot.fullscreen
        self._append(step)

    # --- clicks
    def _handle_click(self, ev: _ClickEvent) -> None:
        shot = ev.shot
        info = self._probe(ev.x, ev.y, shot)
        extra: list[Rect] = []
        if info and info.is_password and info.rect:
            extra.append(info.rect)
        if self._buf.tokens and self._buf.secure and self._last_secure_rect:
            extra.append(self._last_secure_rect)
        self._prepare_shot(shot, extra)
        self._focus_cache = (0.0, False)
        self._flush_typing(shot)            # typing that led up to this click comes first

        cx, cy = ev.x - shot.left, ev.y - shot.top
        step = Step(kind="click", button=ev.button, screen_x=ev.x, screen_y=ev.y,
                    click_x=cx, click_y=cy, window_title=shot.title, fullscreen=shot.fullscreen)
        if info:
            step.element_name = info.name
            step.control_type = info.ctype
        box = self._element_box(info, shot, cx, cy)
        if box is not None:
            step.annotations.append(Annotation("highlight", *box, auto=True))
        else:
            r = 24
            step.annotations.append(Annotation("circle", cx - r, cy - r, cx + r, cy + r, auto=True))
        step.image = self._store(shot)
        self._append(step)

    def _handle_double(self) -> None:
        step = self._last_step
        if step is not None and step.kind == "click" and step.button == "left":
            step.button = "double"
            self.session.dirty = True
            self._on_step(step)

    def _append(self, step: Step) -> None:
        self.session.add_step(step)
        self._last_step = step
        self._on_step(step)

    def _store(self, shot: Shot) -> str:
        if shot.filename is None:
            shot.filename = self.session.save_image(shot.img)
        return shot.filename

    def _element_box(self, info: Optional[ElementInfo], shot: Shot, cx: int, cy: int):
        if not info or not info.rect:
            return None
        l, t, r, b = info.rect
        l, r = l - shot.left, r - shot.left
        t, b = t - shot.top, b - shot.top
        w, h = r - l, b - t
        iw, ih = shot.img.size
        if w <= 0 or h <= 0 or w * h > self.cfg.max_element_fraction * iw * ih:
            return None                     # e.g. the whole document pane: not a useful highlight
        if not (l - 4 <= cx <= r + 4 and t - 4 <= cy <= b + 4):
            return None
        if w < 16:
            l, r = l - (16 - w) / 2, r + (16 - w) / 2
        if h < 16:
            t, b = t - (16 - h) / 2, b + (16 - h) / 2
        return (max(0, l), max(0, t), min(iw, r), min(ih, b))

    # --- UI Automation
    def _probe(self, x: int, y: int, shot: Shot) -> Optional[ElementInfo]:
        """Name / type / bounds of the element under the cursor, or None."""
        if auto is None:
            return None
        try:
            ctrl = auto.ControlFromPoint(x, y)
            if ctrl is None:
                return None
            info = ElementInfo(name=_clean(ctrl.Name), ctype=_ctype(ctrl), rect=_rect_of(ctrl))
            with contextlib.suppress(Exception):
                info.is_password = bool(ctrl.IsPassword)
            if not info.name:
                # icons / labels inside buttons and list items: borrow the parent's name
                node = ctrl
                limit = self.cfg.max_element_fraction * shot.img.size[0] * shot.img.size[1]
                for _ in range(2):
                    node = node.GetParentControl()
                    if node is None or getattr(node, "ControlTypeName", "") not in _NAMEABLE_PARENTS:
                        break
                    name = _clean(node.Name)
                    if name:
                        info.name, info.ctype = name, _ctype(node)
                        prect = _rect_of(node)
                        if prect and _area(prect) <= limit:
                            info.rect = prect
                        break
            return info
        except Exception:
            log.debug("UI Automation lookup failed", exc_info=True)
            return None

    def _scan_password_rects(self, hwnd: int) -> list[Rect]:
        """All password fields in a window, via one server-side UIA search."""
        if auto is None or not hwnd:
            return []
        t0 = time.monotonic()
        rects: list[Rect] = []
        try:
            root = auto.ControlFromHandle(hwnd)
            client = _uia_client()
            cond = client.CreatePropertyCondition(30019, True)      # UIA_IsPasswordPropertyId
            found = root.Element.FindAll(4, cond)                   # TreeScope_Descendants
            for i in range(found.Length):
                r = found.GetElement(i).CurrentBoundingRectangle
                if r.right - r.left > 0 and r.bottom - r.top > 0:
                    rects.append((r.left, r.top, r.right, r.bottom))
        except Exception:
            if not self._scan_warned:       # never fail silently: a broken scan means unblurred passwords
                self._scan_warned = True
                log.warning("Password-field scan failed; only clicked fields will be blurred", exc_info=True)
        if time.monotonic() - t0 > 1.0:
            self._scan_ok = False           # this app's UIA tree is too slow; stop scanning
            log.warning("Password-field scan is slow here; disabled for this recording")
        return rects

    def _prepare_shot(self, shot: Shot, extra_rects: list[Rect]) -> None:
        """Irreversibly blur password fields before the screenshot touches the disk."""
        rects = list(extra_rects)
        if not shot.scanned:
            shot.scanned = True
            if self.cfg.scan_password_fields and self._scan_ok:
                rects += self._scan_password_rects(shot.hwnd)
        for l, t, r, b in rects:
            annotate.blur_region(shot.img, (l - shot.left - 4, t - shot.top - 4,
                                            r - shot.left + 4, b - shot.top + 4),
                                 self.cfg.blur_strength)
