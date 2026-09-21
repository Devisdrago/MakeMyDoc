"""Headless tests for the parts that do not need a desktop: model, annotate, export."""
import os
import sys

import pytest
from PIL import Image, ImageDraw

from makemydoc import annotate, export
from makemydoc.config import DEFAULT_LABELS, Config, parse_hotkey
from makemydoc.session import Annotation, Session, Step, auto_text, step_text


def make_session(tmp_path, n_clicks=2) -> Session:
    session = Session(title="Test guide", directory=str(tmp_path / "session"))
    img = Image.new("RGB", (900, 560), (230, 235, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([300, 200, 420, 240], fill=(40, 90, 200))
    d.text((310, 212), "Save", fill="white")
    name = session.save_image(img)
    for i in range(n_clicks):
        step = Step(kind="click", element_name="Save", control_type="Button", screen_x=350, screen_y=220,
                    click_x=350, click_y=220, image=name)
        step.annotations.append(Annotation("highlight", 300, 200, 420, 240, auto=True))
        session.add_step(step)
    session.add_step(Step(kind="type", typed="report.txt", image=name))
    session.add_step(Step(kind="note", note_level="warning", custom_text="Do not close the window."))
    return session


# ----------------------------------------------------------------- config
def test_parse_hotkey():
    assert parse_hotkey("Ctrl+Shift+P") == (frozenset({"ctrl", "shift"}), "p")
    for bad in ("p", "ctrl", "foo+p", "ctrl+shift"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


# ---------------------------------------------------------------- session
def test_step_text_templates():
    labels = dict(DEFAULT_LABELS)
    click = Step(kind="click", element_name="Save", screen_x=1, screen_y=2)
    assert auto_text(click, labels) == "Left click on: Save"
    click.button = "right"
    assert auto_text(click, labels) == "Right click on: Save"
    click.button, click.element_name = "double", ""
    assert auto_text(click, labels) == "Double click at (1, 2)"
    assert auto_text(Step(kind="type", typed="hi"), labels) == "Type: hi"
    hidden = auto_text(Step(kind="type", secret=True), labels)
    assert "hidden" in hidden

    labels["left"] = "Click {name}!"
    assert auto_text(Step(kind="click", element_name="OK"), labels) == "Click OK!"
    labels["left"] = "broken {oops"
    assert auto_text(Step(kind="click", element_name="OK"), labels) == "Left click on: OK"

    custom = Step(kind="click", element_name="OK", custom_text="Press OK")
    assert step_text(custom, labels) == "Press OK"


def test_numbering_skips_notes(tmp_path):
    session = make_session(tmp_path)
    nums = session.numbers()
    assert sorted(nums.values()) == [1, 2, 3]
    assert session.numbered_count() == 3


def test_undo(tmp_path):
    session = make_session(tmp_path)
    before = len(session.steps)
    session.push_undo()
    del session.steps[0]
    assert session.undo()
    assert len(session.steps) == before
    assert not session.undo()


def test_json_roundtrip(tmp_path):
    session = make_session(tmp_path)
    session.steps[0].custom_text = "Custom"
    path = str(tmp_path / "s.json")
    session.save(path)
    loaded = Session.load(path)
    try:
        assert [s.kind for s in loaded.steps] == [s.kind for s in session.steps]
        assert loaded.steps[0].custom_text == "Custom"
        assert loaded.steps[0].annotations[0].kind == "highlight"
        assert loaded.image_path(loaded.steps[0]) is not None
    finally:
        loaded.cleanup()


def test_load_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": 1}')
    with pytest.raises(ValueError):
        Session.load(str(bad))


# ---------------------------------------------------------------- annotate
def test_blur_region_destroys_detail():
    img = Image.new("RGB", (200, 100), "white")
    d = ImageDraw.Draw(img)
    for x in range(20, 180, 4):
        d.line([(x, 20), (x, 60)], fill="black")
    before = img.crop((20, 20, 180, 60)).tobytes()
    annotate.blur_region(img, (20, 20, 180, 60), 12)
    assert img.crop((20, 20, 180, 60)).tobytes() != before
    assert img.getpixel((5, 5)) == (255, 255, 255)          # outside stays untouched


def test_render_step_adds_bar_and_inset(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    step = session.steps[0]
    raw = Image.open(session.image_path(step)).convert("RGB")
    out = annotate.render_step(raw, step, "Left click on: Save", cfg)
    assert out.width >= raw.width and out.height > raw.height       # text bar + inset

    step.show_inset = False
    no_inset = annotate.render_step(raw, step, "Left click on: Save", cfg)
    assert no_inset.size[0] == raw.width
    assert no_inset.height < out.height


def test_manual_annotations_render(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    step = session.steps[0]
    step.annotations += [
        Annotation("arrow", 100, 100, 290, 210),
        Annotation("blur", 500, 300, 700, 400),
        Annotation("warning", 450, 100, 600, 160, text="Careful here"),
        Annotation("callout", 50, 400, 300, 470, text="Remember to save"),
    ]
    raw = Image.open(session.image_path(step)).convert("RGB")
    assert annotate.render_step(raw, step, "x", cfg).size[0] > 0


# ------------------------------------------------------------------ export
def test_export_all_formats(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    out = tmp_path / "out"
    paths = export.export_all(session, cfg, ["md", "html", "pdf"], str(out), "My *Guide*")
    names = sorted(os.path.basename(p) for p in paths)
    assert names == ["My Guide.html", "My Guide.md", "My Guide.pdf"]

    md = (out / "My Guide.md").read_text(encoding="utf-8")
    assert "## Step 1" in md and "## Step 3" in md
    assert "Table of contents" not in md
    assert "Do not close the window." in md and "Warning" in md
    assert (out / "My Guide_files" / "step_001.png").is_file()

    page = (out / "My Guide.html").read_text(encoding="utf-8")
    assert "<title>My *Guide*</title>" in page
    assert 'id="step-1"' in page and "data:image/png;base64," in page
    assert "Table of contents" not in page

    assert (out / "My Guide.pdf").read_bytes().startswith(b"%PDF")


def test_export_requires_steps(tmp_path):
    with pytest.raises(ValueError):
        export.export_all(Session(directory=str(tmp_path / "s")), Config(), ["md"], str(tmp_path), "t")


def test_html_escapes_user_text(tmp_path):
    session = make_session(tmp_path, n_clicks=1)
    session.add_step(Step(kind="note", custom_text="<script>alert(1)</script>"))
    out = tmp_path / "o"
    export.export_all(session, Config(), ["html"], str(out), "t")
    page = (out / "t.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ----------------------------------------------------------------- capture
@pytest.mark.skipif(sys.platform != "win32", reason="capture needs Windows")
def test_key_grouping_tokens():
    from pynput import keyboard
    from makemydoc.capture import Recorder

    ke = Recorder._key_event
    ev = ke(keyboard.KeyCode.from_char("a"), "a", frozenset())
    assert (ev.kind, ev.value) == ("char", "a")
    ev = ke(keyboard.KeyCode.from_char("A"), "a", frozenset({"shift"}))
    assert (ev.kind, ev.value) == ("char", "A")
    ev = ke(keyboard.KeyCode.from_vk(0x53), "s", frozenset({"ctrl"}))
    assert ev.value == "[Ctrl+S]"
    assert ke(keyboard.Key.enter, "enter", frozenset()).value == "[Enter]"
    assert ke(keyboard.Key.backspace, "backspace", frozenset()).kind == "bs"
    assert ke(keyboard.Key.caps_lock, "caps_lock", frozenset()) is None
    assert ke(keyboard.Key.left, "left", frozenset()) is None


def test_description_not_repeated_below_image_by_default(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    out = tmp_path / "o"
    export.export_all(session, cfg, ["md", "html"], str(out), "t")
    md = (out / "t.md").read_text(encoding="utf-8")
    assert md.count("Left click on: Save") == 0           # the text bar in the picture carries it
    assert md.count("Type: report.txt") == 0
    assert md.count("Do not close the window.") == 1          # notes stay as text

    cfg.text_below_image = True
    export.export_all(session, cfg, ["md"], str(out), "t")
    md = (out / "t.md").read_text(encoding="utf-8")
    assert md.count("Left click on: Save") == 2


# -------------------------------------------------------------------- crop
def test_crop_box_validation():
    assert annotate.crop_box(None, (900, 560)) is None
    assert annotate.crop_box([0, 0, 900, 560], (900, 560)) is None          # full image = no crop
    assert annotate.crop_box([10, 10, 14, 300], (900, 560)) is None         # too thin
    assert annotate.crop_box([-50, -5, 2000, 300], (900, 560)) == (0, 0, 900, 300)
    assert annotate.crop_box([300, 200, 100, 50], (900, 560)) == (100, 50, 300, 200)


def test_crop_changes_output_and_is_reversible(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    step = session.steps[0]
    step.show_inset = False
    raw = Image.open(session.image_path(step)).convert("RGB")
    full = annotate.render_step(raw, step, "", cfg)               # empty text: no bar
    step.crop = [200.0, 150.0, 500.0, 350.0]
    cropped = annotate.render_step(raw, step, "", cfg)
    assert full.size == raw.size
    assert cropped.size == (300, 200)
    step.crop = None
    assert annotate.render_step(raw, step, "", cfg).size == raw.size


def test_crop_drops_inset_when_target_outside(tmp_path):
    session = make_session(tmp_path)
    cfg = Config()
    step = session.steps[0]                                       # highlight at 300..420 x 200..240
    raw = Image.open(session.image_path(step)).convert("RGB")
    step.crop = [0.0, 300.0, 900.0, 560.0]                        # excludes the highlight
    out = annotate.render_step(raw, step, "", cfg)
    assert out.size == (900, 260)                                 # no inset added


def test_crop_survives_session_roundtrip(tmp_path):
    session = make_session(tmp_path)
    session.steps[0].crop = [10.0, 20.0, 400.0, 300.0]
    path = str(tmp_path / "c.json")
    session.save(path)
    loaded = Session.load(path)
    try:
        assert loaded.steps[0].crop == [10.0, 20.0, 400.0, 300.0]
        assert loaded.steps[1].crop is None
    finally:
        loaded.cleanup()
