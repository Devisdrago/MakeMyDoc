"""Export a session to PDF, Markdown and HTML."""
from __future__ import annotations

import base64
import datetime
import html
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote

from PIL import Image

from . import annotate
from .config import Config
from .session import Session, Step, step_text

FORMATS = {"pdf": ".pdf", "md": ".md", "html": ".html"}
FORMAT_NAMES = {"pdf": "PDF", "md": "Markdown", "html": "HTML"}

Progress = Callable[[int, int, str], None]


@dataclass
class Item:
    kind: str                   # step | note | warning
    text: str
    number: Optional[int] = None
    image: Optional[str] = None  # path to the rendered PNG
    anchor: str = ""


def date_text(when: Optional[datetime.date] = None) -> str:
    d = when or datetime.date.today()
    return f"{d:%B} {d.day}, {d.year}"


def safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip(" .")
    return name[:100] or "MakeMyDoc guide"


# --------------------------------------------------------------- rendering
def build_items(session: Session, steps: list[Step], cfg: Config, workdir: str,
                progress: Optional[Progress] = None, total: Optional[int] = None) -> list[Item]:
    """Render every step's annotated picture once (PNG in `workdir`)."""
    items: list[Item] = []
    n = 0
    total = total or len(steps)
    for i, step in enumerate(steps):
        text = step_text(step, cfg.labels)
        if step.kind == "note":
            items.append(Item("warning" if step.note_level == "warning" else "note", text,
                              anchor=f"note-{i + 1}"))
            continue
        n += 1
        image_path = None
        src = session.image_path(step)
        if src:
            img = annotate.render_step_file(src, step, text, cfg)
            image_path = os.path.join(workdir, f"step_{n:03d}.png")
            img.save(image_path, format="PNG")
        items.append(Item("step", text, number=n, image=image_path, anchor=f"step-{n}"))
        if progress:
            progress(i + 1, total, f"Rendering step {n}")
    return items


# ---------------------------------------------------------------- markdown
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|])")


def md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text).replace("\n", "  \n")


def export_markdown(items: list[Item], title: str, out_path: str, text_below: bool = False) -> str:
    stem = os.path.splitext(os.path.basename(out_path))[0]
    assets = os.path.join(os.path.dirname(out_path), f"{stem}_files")
    lines = [f"# {md_escape(title)}", "", f"_{date_text()}_", "", "---", ""]
    for it in items:
        if it.kind == "step":
            lines += [f'<a id="{it.anchor}"></a>', "", f"## Step {it.number}", ""]
            if it.image:
                os.makedirs(assets, exist_ok=True)
                fname = f"step_{it.number:03d}.png"
                shutil.copyfile(it.image, os.path.join(assets, fname))
                lines += [f"![Step {it.number}]({quote(stem + '_files')}/{fname})", ""]
            if it.text and (text_below or not it.image):
                lines += [md_escape(it.text), ""]
        else:
            label = "⚠️ Warning" if it.kind == "warning" else "Note"
            parts = md_escape(it.text).split("  \n")
            lines += [f"> **{label}:** " + "  \n> ".join(parts), ""]
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    return out_path


# -------------------------------------------------------------------- html
_CSS = """
:root{--fg:#1f2937;--muted:#6b7280;--line:#e5e7eb;--accent:#ff2d2d}
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 "Segoe UI",system-ui,-apple-system,Arial,sans-serif;color:var(--fg);background:#f3f4f6}
main{max-width:980px;margin:0 auto;padding:32px 20px 80px}
.title-page{background:#fff;border:1px solid var(--line);border-radius:10px;padding:80px 32px;text-align:center;margin-bottom:24px}
.title-page h1{margin:0 0 12px;font-size:2.2rem}
.title-page p{margin:0;color:var(--muted)}
section.step,.note{background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px 24px;margin-bottom:20px}
section.step h2{margin:0 0 14px;font-size:1.25rem;border-left:5px solid var(--accent);padding-left:10px}
section.step img{display:block;max-width:100%;height:auto;border-radius:6px;border:1px solid var(--line)}
.desc{margin:14px 0 0;font-size:1.05rem}
.note{background:#eef4ff;border-color:#c7d7fb}
.note.warning{background:#fff7d6;border-color:#f2c94c}
@media print{body{background:#fff}main{padding:0}section.step,.note{break-inside:avoid;border:0}
.title-page{border:0;page-break-after:always}}
"""


def _br(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def export_html(items: list[Item], title: str, out_path: str, embed_images: bool = True,
                text_below: bool = False) -> str:
    stem = os.path.splitext(os.path.basename(out_path))[0]
    assets = os.path.join(os.path.dirname(out_path), f"{stem}_files")
    body: list[str] = []
    for it in items:
        if it.kind == "step":
            img = ""
            if it.image:
                if embed_images:
                    with open(it.image, "rb") as fh:
                        src = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
                else:
                    os.makedirs(assets, exist_ok=True)
                    fname = f"step_{it.number:03d}.png"
                    shutil.copyfile(it.image, os.path.join(assets, fname))
                    src = f"{quote(stem + '_files')}/{fname}"
                img = f'<img src="{src}" alt="Step {it.number}">'
            show = it.text and (text_below or not it.image)
            desc = f'<p class="desc">{_br(it.text)}</p>' if show else ""
            body.append(f'<section class="step" id="{it.anchor}"><h2>Step {it.number}</h2>{img}{desc}</section>')
        else:
            label = "Warning" if it.kind == "warning" else "Note"
            cls = "note warning" if it.kind == "warning" else "note"
            body.append(f'<div class="{cls}"><strong>{label}:</strong> {_br(it.text)}</div>')
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<header class="title-page"><h1>{html.escape(title)}</h1><p>{html.escape(date_text())}</p></header>
{chr(10).join(body)}
</main>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return out_path


# --------------------------------------------------------------------- pdf
def _new_pdf():
    from fpdf import FPDF

    class _PDF(FPDF):
        mmd_font = "Helvetica"
        unicode_ok = False

        def footer(self):
            if self.page_no() == 1:
                return
            self.set_y(-12)
            self.set_font(self.mmd_font, "", 9)
            self.set_text_color(120, 120, 120)
            self.cell(0, 8, f"Page {self.page_no()}", align="C")

    pdf = _PDF(orientation="P", unit="mm", format="A4")
    reg, bold = annotate.find_font_file(False), annotate.find_font_file(True)
    if reg and reg.lower().endswith(".ttf"):
        try:
            pdf.add_font("MMD", "", reg)
            pdf.add_font("MMD", "B", bold or reg)
            pdf.mmd_font, pdf.unicode_ok = "MMD", True
        except Exception:
            pdf.mmd_font, pdf.unicode_ok = "Helvetica", False
    return pdf


def _build_pdf(items: list[Item], title: str, cfg: Config, tmp: str):
    from fpdf.enums import XPos, YPos

    pdf = _new_pdf()
    font = pdf.mmd_font
    NEXT = dict(new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def t(s: str) -> str:
        return s if pdf.unicode_ok else s.encode("latin-1", "replace").decode("latin-1")

    def fit(s: str, width: float) -> str:
        s = s.split("\n")[0]
        if pdf.get_string_width(t(s)) <= width:
            return s
        while len(s) > 1 and pdf.get_string_width(t(s + "...")) > width:
            s = s[:-1]
        return s + "..."

    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title(t(title))
    pdf.set_creator("MakeMyDoc")
    bottom = pdf.h - 18

    # title page
    pdf.add_page()
    pdf.set_y(90)
    pdf.set_font(font, "B", 30)
    pdf.set_text_color(31, 41, 55)
    pdf.multi_cell(0, 14, t(title), align="C", **NEXT)
    pdf.ln(6)
    pdf.set_font(font, "", 13)
    pdf.set_text_color(107, 114, 128)
    pdf.cell(0, 8, t(date_text()), align="C", **NEXT)

    # steps
    pdf.add_page()
    for it in items:
        if it.kind != "step":
            fill = (255, 247, 214) if it.kind == "warning" else (232, 240, 254)
            label = "Warning" if it.kind == "warning" else "Note"
            if pdf.get_y() + 24 > bottom:
                pdf.add_page()
            pdf.set_fill_color(*fill)
            pdf.set_font(font, "", 11)
            pdf.set_text_color(31, 41, 55)
            pdf.multi_cell(0, 6.5, t(f"{label}: {it.text}"), fill=True, **NEXT)
            pdf.ln(6)
            continue

        w_mm = h_mm = 0.0
        jpg = None
        if it.image:
            with Image.open(it.image) as im:
                wpx, hpx = im.size
                w_mm = pdf.epw
                h_mm = w_mm * hpx / wpx
                max_h = bottom - 15 - 9 - 24
                if h_mm > max_h:
                    w_mm, h_mm = w_mm * max_h / h_mm, max_h
                jpg = os.path.join(tmp, os.path.splitext(os.path.basename(it.image))[0] + ".jpg")
                if not os.path.isfile(jpg):
                    im.convert("RGB").save(jpg, format="JPEG", quality=90)
        if pdf.get_y() + 9 + h_mm + 22 > bottom:
            pdf.add_page()
        pdf.set_font(font, "B", 15)
        pdf.set_text_color(31, 41, 55)
        pdf.cell(0, 9, f"Step {it.number}", **NEXT)
        if jpg:
            y = pdf.get_y()
            pdf.image(jpg, x=pdf.l_margin + (pdf.epw - w_mm) / 2, y=y, w=w_mm, h=h_mm)
            pdf.set_y(y + h_mm + 3)
        if it.text and (cfg.text_below_image or not jpg):
            pdf.set_font(font, "", 12)
            pdf.multi_cell(0, 6.5, t(it.text), **NEXT)
        pdf.ln(7)
    return pdf


def export_pdf(items: list[Item], title: str, out_path: str, cfg: Config, tmp: str) -> str:
    _build_pdf(items, title, cfg, tmp).output(out_path)
    return out_path


# ------------------------------------------------------------------ driver
def export_all(session: Session, cfg: Config, formats: list[str], folder: str, title: str,
               progress: Optional[Progress] = None, steps: Optional[list[Step]] = None) -> list[str]:
    """Export to every requested format; returns the written file paths."""
    steps = list(session.steps if steps is None else steps)
    if not steps:
        raise ValueError("There are no steps to export.")
    unknown = [f for f in formats if f not in FORMATS]
    if unknown or not formats:
        raise ValueError("Choose at least one valid export format.")
    os.makedirs(folder, exist_ok=True)
    stem = safe_filename(title)
    total = len(steps) + len(formats)
    work = tempfile.mkdtemp(prefix="mmd_export_")
    outputs: list[str] = []
    try:
        items = build_items(session, steps, cfg, work, progress, total)
        done = len(steps)
        for fmt in formats:
            path = os.path.join(folder, stem + FORMATS[fmt])
            if progress:
                progress(done, total, f"Writing {FORMAT_NAMES[fmt]}")
            if fmt == "md":
                export_markdown(items, title, path, cfg.text_below_image)
            elif fmt == "html":
                export_html(items, title, path, cfg.html_embed_images, cfg.text_below_image)
            else:
                export_pdf(items, title, path, cfg, work)
            outputs.append(path)
            done += 1
        if progress:
            progress(total, total, "Done")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return outputs
