"""User-editable settings: label templates, hotkeys and rendering options."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "MakeMyDoc"

# Step text templates. Placeholders: {name} {type} {x} {y} {text}
DEFAULT_LABELS: dict[str, str] = {
    "left": "Left click on: {name}",
    "right": "Right click on: {name}",
    "double": "Double click on: {name}",
    "left_at": "Left click at ({x}, {y})",
    "right_at": "Right click at ({x}, {y})",
    "double_at": "Double click at ({x}, {y})",
    "type": "Type: {text}",
    "hidden_text": "(hidden - password field)",
}

LABEL_CAPTIONS: dict[str, str] = {
    "left": "Left click (element has a name)",
    "right": "Right click (element has a name)",
    "double": "Double click (element has a name)",
    "left_at": "Left click (no name - uses coordinates)",
    "right_at": "Right click (no name - uses coordinates)",
    "double_at": "Double click (no name - uses coordinates)",
    "type": "Typed text",
    "hidden_text": "Text shown instead of a typed password",
}

LABEL_PLACEHOLDERS: dict[str, str] = {
    "left": "{name} {type}",
    "right": "{name} {type}",
    "double": "{name} {type}",
    "left_at": "{x} {y}",
    "right_at": "{x} {y}",
    "double_at": "{x} {y}",
    "type": "{text}",
    "hidden_text": "",
}

DEFAULT_HOTKEY_PAUSE = "ctrl+shift+p"
DEFAULT_HOTKEY_STOP = "ctrl+shift+s"

_MODIFIERS = ("ctrl", "alt", "shift", "win")


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def temp_root() -> str:
    return os.path.join(tempfile.gettempdir(), APP_NAME)


def parse_hotkey(spec: str) -> tuple[frozenset[str], str]:
    """Parse 'ctrl+shift+p' into ({'ctrl','shift'}, 'p'). Raises ValueError."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("A hotkey needs at least one modifier and a key, e.g. ctrl+shift+p")
    *mods, key = parts
    for m in mods:
        if m not in _MODIFIERS:
            raise ValueError(f"Unknown modifier '{m}'. Use: {', '.join(_MODIFIERS)}")
    if key in _MODIFIERS:
        raise ValueError("The last part must be a normal key, not a modifier")
    return frozenset(mods), key


def format_hotkey(spec: str) -> str:
    """'ctrl+shift+p' -> 'Ctrl+Shift+P' for display."""
    return "+".join(p.strip().capitalize() if len(p.strip()) > 1 else p.strip().upper()
                    for p in spec.split("+") if p.strip())


@dataclass
class Config:
    labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    hotkey_pause: str = DEFAULT_HOTKEY_PAUSE
    hotkey_stop: str = DEFAULT_HOTKEY_STOP

    theme: str = "system"               # system | light | dark

    # rendering
    highlight_color: str = "#ff2d2d"
    bar_color: str = "#1f2937"
    bar_text_color: str = "#ffffff"
    inset_layout: str = "auto"          # auto | beside | below | off
    blur_strength: int = 12
    max_element_fraction: float = 0.6   # bigger elements fall back to a circle

    # capture
    scan_password_fields: bool = True
    show_badge: bool = True

    # export
    html_embed_images: bool = True
    text_below_image: bool = False      # the text bar already shows the description
    last_export_dir: str = ""
    last_session_dir: str = ""

    # ------------------------------------------------------------------ io
    @classmethod
    def path(cls) -> str:
        return os.path.join(config_dir(), "config.json")

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            with open(cls.path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cfg
        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in known and key != "labels":
                setattr(cfg, key, value)
        if isinstance(data.get("labels"), dict):
            cfg.labels.update({k: str(v) for k, v in data["labels"].items() if k in DEFAULT_LABELS})
        return cfg

    def save(self) -> None:
        try:
            os.makedirs(config_dir(), exist_ok=True)
            with open(self.path(), "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
        except OSError:
            pass

    def render_key(self) -> str:
        """Fingerprint of everything that changes how a step image looks."""
        return "|".join(str(v) for v in (
            self.highlight_color, self.bar_color, self.bar_text_color,
            self.inset_layout, self.blur_strength))
