"""Integration test: a real Windows password box must be found and blurred.

Opens a small WinForms window via PowerShell, so it needs an interactive Windows desktop.
"""
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows UI Automation")

FORM = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$f = New-Object System.Windows.Forms.Form; $f.Text = 'MmdPwProbe'; $f.TopMost = $true
$f.StartPosition = 'Manual'; $f.Location = New-Object System.Drawing.Point(420, 180)
$f.ClientSize = New-Object System.Drawing.Size(400, 160)
$p = New-Object System.Windows.Forms.TextBox; $p.UseSystemPasswordChar = $true; $p.AccessibleName = 'Secret'
$p.Location = New-Object System.Drawing.Point(30, 40); $p.Size = New-Object System.Drawing.Size(300, 28); $p.Text = 'hunter2!'
$f.Controls.Add($p); [System.Windows.Forms.Application]::Run($f)
"""


@pytest.fixture
def password_form():
    from makemydoc import winutil
    proc = subprocess.Popen(["powershell", "-NoProfile", "-STA", "-Command", FORM])
    hwnd = None
    try:
        for _ in range(40):
            time.sleep(0.5)
            h = winutil.foreground_window()
            if h and winutil.window_title(h) == "MmdPwProbe":
                hwnd = h
                break
        if hwnd is None:
            pytest.skip("could not bring up the probe window (no interactive desktop?)")
        time.sleep(1.0)
        yield hwnd
    finally:
        proc.terminate()


def test_scan_finds_password_field_and_blur_hides_it(password_form):
    import uiautomation as auto
    from PIL import Image
    from makemydoc import capture
    from makemydoc.config import Config

    hwnd = password_form
    with auto.UIAutomationInitializerInThread():
        rec = capture.Recorder(Config())
        rects = rec._scan_password_rects(hwnd)
        edit = auto.ControlFromHandle(hwnd).EditControl(Name="Secret").BoundingRectangle
    assert len(rects) == 1
    l, t, r, b = rects[0]
    assert abs(l - edit.left) <= 2 and abs(t - edit.top) <= 2 and abs(r - edit.right) <= 2

    # the same rectangle, pushed through the capture pipeline, must change the pixels
    shot = capture.capture_active((l + 5, t + 5))
    assert shot is not None
    before = shot.img.copy()
    with auto.UIAutomationInitializerInThread():
        rec._prepare_shot(shot, [])
    box = (l - shot.left, t - shot.top, r - shot.left, b - shot.top)
    assert shot.img.crop(box).tobytes() != before.crop(box).tobytes()
