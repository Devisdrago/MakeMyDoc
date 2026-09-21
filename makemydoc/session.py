"""Data model (steps, annotations) and session storage."""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from PIL import Image

from .config import DEFAULT_LABELS, temp_root

SESSION_VERSION = 1

ANN_KINDS = ("highlight", "circle", "arrow", "blur", "warning", "callout", "crop")


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class Annotation:
    """A shape on a step image, in pixel coordinates of the stored screenshot."""
    kind: str
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""
    auto: bool = False
    id: str = field(default_factory=new_id)

    def box(self) -> tuple[float, float, float, float]:
        return (min(self.x0, self.x1), min(self.y0, self.y1),
                max(self.x0, self.x1), max(self.y0, self.y1))


@dataclass
class Step:
    kind: str = "click"                 # click | type | note
    button: str = "left"                # left | right | double
    element_name: str = ""
    control_type: str = ""
    screen_x: Optional[int] = None
    screen_y: Optional[int] = None
    click_x: Optional[int] = None       # click position inside the screenshot
    click_y: Optional[int] = None
    typed: str = ""
    secret: bool = False                # typed into a password field; text never stored
    note_level: str = "note"            # note | warning (kind == "note")
    custom_text: Optional[str] = None   # None = generated from the label templates
    image: Optional[str] = None         # file name inside the session folder
    annotations: list[Annotation] = field(default_factory=list)
    show_inset: bool = True
    crop: Optional[list[float]] = None  # [left, top, right, bottom] in screenshot pixels
    window_title: str = ""
    fullscreen: bool = False            # active-window capture unavailable
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=new_id)

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known and k != "annotations"}
        step = cls(**clean)
        ann_fields = {f.name for f in fields(Annotation)}
        step.annotations = [
            Annotation(**{k: v for k, v in a.items() if k in ann_fields})
            for a in data.get("annotations", [])
        ]
        return step


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def _format(template: str, fallback: str, **values) -> str:
    try:
        return template.format_map(_SafeDict(**values))
    except (ValueError, IndexError, KeyError, AttributeError):
        return fallback.format_map(_SafeDict(**values))


def auto_text(step: Step, labels: dict[str, str]) -> str:
    """Text generated from the label templates (ignores custom_text)."""
    if step.kind == "note":
        return ""
    if step.kind == "type":
        text = labels.get("hidden_text", DEFAULT_LABELS["hidden_text"]) if step.secret else step.typed
        return _format(labels.get("type", DEFAULT_LABELS["type"]), DEFAULT_LABELS["type"], text=text)
    btn = step.button if step.button in ("left", "right", "double") else "left"
    if step.element_name:
        key = btn
    else:
        key = btn + "_at"
    return _format(labels.get(key, DEFAULT_LABELS[key]), DEFAULT_LABELS[key],
                   name=step.element_name, type=step.control_type,
                   x=step.screen_x if step.screen_x is not None else "?",
                   y=step.screen_y if step.screen_y is not None else "?")


def step_text(step: Step, labels: dict[str, str]) -> str:
    """The text shown for a step: the user's edit if any, else the generated text."""
    if step.custom_text is not None:
        return step.custom_text
    return auto_text(step, labels)


class Session:
    """A recording: ordered steps plus the screenshots stored in a temp folder."""

    def __init__(self, title: str = "Untitled guide", directory: Optional[str] = None):
        self.title = title
        self.created = time.time()
        self.steps: list[Step] = []
        self._dir = directory
        self._counter = 0
        self.dirty = False
        self.undo_stack: list[list[Step]] = []

    # ------------------------------------------------------------- storage
    @property
    def dir(self) -> str:
        if self._dir is None:
            root = temp_root()
            os.makedirs(root, exist_ok=True)
            self._dir = os.path.join(root, time.strftime("session_%Y%m%d_%H%M%S_") + new_id()[:4])
        os.makedirs(self._dir, exist_ok=True)
        return self._dir

    @property
    def has_storage(self) -> bool:
        return self._dir is not None and os.path.isdir(self._dir)

    def save_image(self, img: Image.Image) -> str:
        self._counter += 1
        name = f"shot_{self._counter:04d}_{new_id()[:4]}.png"
        img.save(os.path.join(self.dir, name), format="PNG", compress_level=1)
        return name

    def image_path(self, step: Step) -> Optional[str]:
        if not step.image:
            return None
        path = os.path.join(self.dir, step.image)
        return path if os.path.isfile(path) else None

    def cleanup(self) -> None:
        """Delete this session's temp folder and everything in it."""
        if self._dir and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self.steps.clear()
        self.undo_stack.clear()
        self.dirty = False

    @staticmethod
    def cleanup_all_temp(keep: Optional[str] = None) -> int:
        """Delete every MakeMyDoc temp session folder except `keep`. Returns count."""
        root = temp_root()
        if not os.path.isdir(root):
            return 0
        removed = 0
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path) and name.startswith("session_") and \
                    os.path.abspath(path) != os.path.abspath(keep or ""):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed

    # ---------------------------------------------------------------- steps
    def add_step(self, step: Step) -> Step:
        self.steps.append(step)
        self.dirty = True
        return step

    def numbers(self) -> dict[str, int]:
        """Step id -> visible step number (notes are not numbered)."""
        out: dict[str, int] = {}
        n = 0
        for s in self.steps:
            if s.kind != "note":
                n += 1
                out[s.id] = n
        return out

    def numbered_count(self) -> int:
        return sum(1 for s in self.steps if s.kind != "note")

    # ----------------------------------------------------------------- undo
    def push_undo(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.steps))
        del self.undo_stack[:-100]
        self.dirty = True

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.steps = self.undo_stack.pop()
        self.dirty = True
        return True

    # ------------------------------------------------------------ json i/o
    def to_dict(self, embed_images: bool = True) -> dict:
        used = {s.image for s in self.steps if s.image}
        images: dict[str, str] = {}
        if embed_images:
            for name in sorted(used):
                path = os.path.join(self.dir, name)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        images[name] = base64.b64encode(fh.read()).decode("ascii")
        return {
            "version": SESSION_VERSION,
            "title": self.title,
            "created": self.created,
            "steps": [asdict(s) for s in self.steps],
            "images": images,
        }

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh)
        os.replace(tmp, path)
        self.dirty = False

    @classmethod
    def load(cls, path: str) -> "Session":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "steps" not in data:
            raise ValueError("This file is not a MakeMyDoc session.")
        if int(data.get("version", 1)) > SESSION_VERSION:
            raise ValueError("This session was saved by a newer version of MakeMyDoc.")
        session = cls(title=data.get("title", "Untitled guide"))
        session.created = data.get("created", time.time())
        for name, b64 in data.get("images", {}).items():
            safe = os.path.basename(name)          # never trust paths from a file
            with open(os.path.join(session.dir, safe), "wb") as out:
                out.write(base64.b64decode(b64))
        session.steps = [Step.from_dict(d) for d in data["steps"]]
        for st in session.steps:
            if st.image:
                st.image = os.path.basename(st.image)
        session._counter = len(data.get("images", {}))
        session.dirty = False
        return session


def image_size(path: str) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
