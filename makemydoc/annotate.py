"""Image processing: blur, highlights, callouts, zoom inset and the step text bar."""
from __future__ import annotations

import hashlib
import math
import os
from functools import lru_cache
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .session import Annotation, Step

Box = tuple[float, float, float, float]

_LANCZOS = Image.Resampling.LANCZOS
_BG = (245, 246, 248)
_AMBER = (245, 158, 11)
_AMBER_DARK = (120, 53, 15)
_NOTE_FILL = (255, 247, 214)
_CALLOUT_FILL = (255, 248, 197)
_CALLOUT_LINE = (212, 167, 44)
_BLUE = (37, 99, 235)


# ------------------------------------------------------------------ helpers
def hex_rgb(color: str, default: tuple[int, int, int] = (255, 45, 45)) -> tuple[int, int, int]:
    c = (color or "").lstrip("#")
    try:
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except (ValueError, IndexError):
        return default


def _font_dirs() -> list[str]:
    win = os.environ.get("WINDIR", r"C:\Windows")
    return [os.path.join(win, "Fonts"), "/usr/share/fonts/truetype/dejavu",
            "/usr/share/fonts/truetype/liberation", "/Library/Fonts"]


_REGULAR = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
_BOLD = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")


def find_font_file(bold: bool = False) -> Optional[str]:
    for name in (_BOLD if bold else _REGULAR):
        for d in _font_dirs():
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


@lru_cache(maxsize=64)
def get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = find_font_file(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # very old Pillow
        return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while len(word) > 1 and draw.textlength(word, font=font) > max_w:
                n = len(word)
                while n > 1 and draw.textlength(word[:n], font=font) > max_w:
                    n -= 1
                lines.append(word[:n])
                word = word[n:]
            cur = word
        lines.append(cur)
    return lines


def _scale(img: Image.Image) -> float:
    return max(1.0, max(img.size) / 1400.0)


# --------------------------------------------------------------------- blur
def blur_region(img: Image.Image, box: Box, strength: int = 12) -> None:
    """Pixelate + blur a rectangle in place. Strong enough to make text unreadable."""
    w_img, h_img = img.size
    l = max(0, int(math.floor(min(box[0], box[2]))))
    t = max(0, int(math.floor(min(box[1], box[3]))))
    r = min(w_img, int(math.ceil(max(box[0], box[2]))))
    b = min(h_img, int(math.ceil(max(box[1], box[3]))))
    if r - l < 2 or b - t < 2:
        return
    region = img.crop((l, t, r, b))
    w, h = region.size
    block = max(int(strength), min(w, h) // 6, 4)
    small = region.resize((max(1, w // block), max(1, h // block)), Image.Resampling.BILINEAR)
    mosaic = small.resize((w, h), Image.Resampling.NEAREST)
    img.paste(mosaic.filter(ImageFilter.GaussianBlur(max(1.0, block / 3))), (l, t))


def render_blurred(raw: Image.Image, annotations: Iterable[Annotation], strength: int) -> Image.Image:
    """Copy of the screenshot with every 'blur' annotation applied."""
    img = raw.convert("RGB").copy()
    for a in annotations:
        if a.kind == "blur":
            blur_region(img, a.box(), strength)
    return img


# --------------------------------------------------------------- annotations
def _dashed_rect(d: ImageDraw.ImageDraw, box: Box, color, width: int, dash: float, gap: float) -> None:
    x0, y0, x1, y1 = box
    for (ax, ay), (bx, by) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                               ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        length = math.hypot(bx - ax, by - ay)
        if length == 0:
            continue
        ux, uy = (bx - ax) / length, (by - ay) / length
        pos = 0.0
        while pos < length:
            end = min(pos + dash, length)
            d.line([(ax + ux * pos, ay + uy * pos), (ax + ux * end, ay + uy * end)],
                   fill=color, width=width)
            pos += dash + gap


def _arrow(d: ImageDraw.ImageDraw, x0, y0, x1, y1, color, s: float) -> None:
    ang = math.atan2(y1 - y0, x1 - x0)
    head, half = 20 * s, 9 * s
    bx, by = x1 - head * math.cos(ang), y1 - head * math.sin(ang)
    d.line([(x0, y0), (bx, by)], fill=color, width=max(2, int(4 * s)))
    d.polygon([(x1, y1),
               (bx + half * math.sin(ang), by - half * math.cos(ang)),
               (bx - half * math.sin(ang), by + half * math.cos(ang))], fill=color)


def _warning_icon(d: ImageDraw.ImageDraw, x: float, y: float, size: float) -> None:
    """Amber triangle with '!' whose bottom-left corner is at (x, y)."""
    top = (x + size / 2, y - size * 0.9)
    d.polygon([top, (x + size, y), (x, y)], fill=_AMBER, outline=_AMBER_DARK)
    font = get_font(max(10, int(size * 0.62)), bold=True)
    d.text((x + size / 2, y - size * 0.32), "!", fill=_AMBER_DARK, font=font, anchor="mm")


def _note_box(d: ImageDraw.ImageDraw, text: str, x: float, y: float, max_w: float,
              s: float, fill, line, img_size) -> tuple[float, float, float, float]:
    """Draw a word-wrapped note box with its top-left at (x, y); returns its bounds."""
    font = get_font(max(12, int(15 * s)))
    pad = 8 * s
    lines = wrap_text(d, text, font, max_w - 2 * pad)
    lh = int(font.size * 1.3) if hasattr(font, "size") else 18
    w = min(max_w, max(d.textlength(ln, font=font) for ln in lines) + 2 * pad)
    h = lh * len(lines) + 2 * pad
    x = min(max(0, x), max(0, img_size[0] - w))
    y = min(max(0, y), max(0, img_size[1] - h))
    d.rounded_rectangle([x, y, x + w, y + h], radius=6 * s, fill=fill + (255,), outline=line + (255,),
                        width=max(1, int(2 * s)))
    for i, ln in enumerate(lines):
        d.text((x + pad, y + pad + i * lh), ln, fill=(36, 41, 47, 255), font=font)
    return (x, y, x + w, y + h)


def draw_annotations(img: Image.Image, annotations: Iterable[Annotation], hl_color) -> Image.Image:
    """Draw every non-blur annotation onto (a copy of) an RGB image."""
    anns = [a for a in annotations if a.kind not in ("blur", "crop")]
    if not anns:
        return img
    s = _scale(img)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    red = tuple(hl_color)
    lw = max(2, int(3 * s))
    for a in anns:
        x0, y0, x1, y1 = a.box()
        if a.kind == "highlight":
            pad = 2 * s
            d.rounded_rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], radius=4 * s,
                                fill=red + (38,), outline=red + (255,), width=lw)
        elif a.kind == "circle":
            d.ellipse([x0, y0, x1, y1], fill=red + (38,), outline=red + (255,), width=lw)
        elif a.kind == "arrow":
            _arrow(d, a.x0, a.y0, a.x1, a.y1, red + (255,), s)
        elif a.kind == "warning":
            _dashed_rect(d, (x0, y0, x1, y1), _AMBER + (255,), lw, 10 * s, 6 * s)
            icon = 28 * s
            iy = y0 if y0 - icon * 0.9 >= 0 else y0 + icon * 0.9 + 2
            _warning_icon(d, x0, iy, icon)
            if a.text.strip():
                max_w = max(220 * s, x1 - x0)
                size = img.size
                _note_box(d, a.text, x0, y1 + 6 * s, max_w, s, _NOTE_FILL, _AMBER, size)
        elif a.kind == "callout":
            font = get_font(max(12, int(16 * s)))
            pad = 8 * s
            lines = wrap_text(d, a.text or " ", font, max(40, (x1 - x0) - 2 * pad))
            lh = int(font.size * 1.3) if hasattr(font, "size") else 18
            y_end = max(y1, y0 + lh * len(lines) + 2 * pad)
            d.rounded_rectangle([x0, y0, x1, y_end], radius=6 * s, fill=_CALLOUT_FILL + (255,),
                                outline=_CALLOUT_LINE + (255,), width=max(1, int(2 * s)))
            for i, ln in enumerate(lines):
                d.text((x0 + pad, y0 + pad + i * lh), ln, fill=(36, 41, 47, 255), font=font)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


# --------------------------------------------------------------------- inset
def focus_target(step: Step) -> Optional[tuple[Box, Optional[str]]]:
    """Area the zoom inset centres on: the highlight if any, else the click point."""
    for a in step.annotations:
        if a.kind in ("highlight", "circle"):
            return a.box(), a.kind
    if step.click_x is not None and step.click_y is not None:
        r = 24
        return (step.click_x - r, step.click_y - r, step.click_x + r, step.click_y + r), None
    return None


def make_inset(img: Image.Image, focus: tuple[Box, Optional[str]], hl_color) -> Optional[Image.Image]:
    """Magnified crop around the focus area, or None when zooming would not help."""
    (fx0, fy0, fx1, fy1), kind = focus
    w_img, h_img = img.size
    fw, fh = max(fx1 - fx0, 1.0), max(fy1 - fy0, 1.0)
    aspect = 0.6
    cw = max(fw * 2.4, 240.0)
    ch = cw * aspect
    if fh * 1.8 > ch:
        ch = fh * 1.8
        cw = ch / aspect
    cw, ch = min(cw, w_img), min(ch, h_img)
    if cw * ch > 0.55 * w_img * h_img:
        return None
    cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    left = min(max(cx - cw / 2, 0), w_img - cw)
    top = min(max(cy - ch / 2, 0), h_img - ch)
    box = (int(left), int(top), int(left + cw), int(top + ch))
    cwi, chi = box[2] - box[0], box[3] - box[1]
    if cwi < 8 or chi < 8:
        return None
    target_w = min(max(cwi * 3, 320), min(w_img * 0.6, 640))
    zoom = target_w / cwi
    if zoom < 1.4:
        return None
    inset = img.crop(box).resize((int(cwi * zoom), int(chi * zoom)), _LANCZOS)
    d = ImageDraw.Draw(inset)
    if kind:
        pad = 2
        shape = [(fx0 - box[0]) * zoom - pad, (fy0 - box[1]) * zoom - pad,
                 (fx1 - box[0]) * zoom + pad, (fy1 - box[1]) * zoom + pad]
        if kind == "circle":
            d.ellipse(shape, outline=tuple(hl_color), width=3)
        else:
            d.rounded_rectangle(shape, radius=4, outline=tuple(hl_color), width=3)
    d.rectangle([0, 0, inset.width - 1, inset.height - 1], outline=(60, 66, 76), width=3)
    return inset


# ------------------------------------------------------------------- compose
def compose(base: Image.Image, text: str, inset: Optional[Image.Image], cfg) -> Image.Image:
    """Text bar on top, screenshot, and the zoom inset beside or below it."""
    w, h = base.size
    layout = getattr(cfg, "inset_layout", "auto")
    if inset is None or layout == "off":
        layout = "none"
    elif layout == "auto":
        layout = "below" if w >= h * 1.15 else "beside"
    gap = max(12, w // 60)
    iw, ih = inset.size if inset is not None else (0, 0)

    if layout == "below":
        canvas_w, body_h = w, h + gap + ih + gap
    elif layout == "beside":
        canvas_w, body_h = w + gap + iw, max(h, ih)
    else:
        canvas_w, body_h = w, h

    bar_h, lines, font, fs = 0, [], None, 0
    text = (text or "").strip()
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    if text:
        fs = int(max(15, min(30, w * 0.019)))
        font = get_font(fs, bold=True)
        pad = int(fs * 0.7)
        lines = wrap_text(probe, text, font, canvas_w - 2 * pad - 10)
        if len(lines) > 3:
            lines = lines[:3]
            while len(lines[2]) > 1 and probe.textlength(lines[2] + "...", font=font) > canvas_w - 2 * pad - 10:
                lines[2] = lines[2][:-1]
            lines[2] += "..."
        bar_h = pad * 2 + int(fs * 1.3) * len(lines)

    canvas = Image.new("RGB", (canvas_w, bar_h + body_h), _BG)
    d = ImageDraw.Draw(canvas)
    if bar_h:
        d.rectangle([0, 0, canvas_w, bar_h], fill=hex_rgb(cfg.bar_color, (31, 41, 55)))
        d.rectangle([0, 0, 6, bar_h], fill=hex_rgb(cfg.highlight_color))
        pad = int(fs * 0.7)
        for i, ln in enumerate(lines):
            d.text((pad + 8, pad + i * int(fs * 1.3)), ln, fill=hex_rgb(cfg.bar_text_color, (255, 255, 255)),
                   font=font)
    canvas.paste(base, (0, bar_h))
    d.rectangle([0, bar_h, w - 1, bar_h + h - 1], outline=(200, 205, 212))
    if layout == "below":
        canvas.paste(inset, ((w - iw) // 2, bar_h + h + gap))
    elif layout == "beside":
        canvas.paste(inset, (w + gap, bar_h))
    return canvas


def crop_box(crop, size: tuple[int, int]) -> Optional[tuple[int, int, int, int]]:
    """Validated integer crop rectangle, or None when there is no effective crop."""
    if not crop or len(crop) != 4:
        return None
    w, h = size
    l = max(0, int(round(min(crop[0], crop[2]))))
    t = max(0, int(round(min(crop[1], crop[3]))))
    r = min(w, int(round(max(crop[0], crop[2]))))
    b = min(h, int(round(max(crop[1], crop[3]))))
    if r - l < 8 or b - t < 8 or (l, t, r, b) == (0, 0, w, h):
        return None
    return (l, t, r, b)


def render_step(raw: Image.Image, step: Step, text: str, cfg) -> Image.Image:
    """The full exported picture of one step."""
    hl = hex_rgb(cfg.highlight_color)
    blurred = render_blurred(raw, step.annotations, cfg.blur_strength)
    base = draw_annotations(blurred, step.annotations, hl)
    box = crop_box(step.crop, raw.size)
    ox = oy = 0
    if box:
        # Crop everything, including what the zoom inset is cut from, so cropped-away
        # content can never leak back into the picture.
        base, blurred = base.crop(box), blurred.crop(box)
        ox, oy = box[0], box[1]
    inset = None
    if step.show_inset and getattr(cfg, "inset_layout", "auto") != "off":
        focus = focus_target(step)
        if focus is not None:
            (x0, y0, x1, y1), kind = focus
            x0, x1, y0, y1 = x0 - ox, x1 - ox, y0 - oy, y1 - oy
            if 0 <= (x0 + x1) / 2 < blurred.width and 0 <= (y0 + y1) / 2 < blurred.height:
                inset = make_inset(blurred, ((x0, y0, x1, y1), kind), hl)
    return compose(base, text, inset, cfg)


def render_step_file(path: str, step: Step, text: str, cfg) -> Image.Image:
    with Image.open(path) as im:
        raw = im.convert("RGB")
    return render_step(raw, step, text, cfg)


def render_key(step: Step, text: str, cfg) -> str:
    """Cache key: changes whenever the rendered picture would change."""
    payload = [text, step.show_inset, step.image, step.click_x, step.click_y, step.crop, cfg.render_key(),
               [(a.kind, round(a.x0, 1), round(a.y0, 1), round(a.x1, 1), round(a.y1, 1), a.text)
                for a in step.annotations]]
    return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()


def thumbnail(img: Image.Image, width: int, max_height: Optional[int] = None) -> Image.Image:
    max_height = max_height or width * 2
    out = img.copy()
    out.thumbnail((width, max_height), _LANCZOS)
    return out
