"""Thin ctypes wrappers over the Win32 calls MakeMyDoc needs."""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

OWN_PID = os.getpid()

Rect = tuple[int, int, int, int]  # left, top, right, bottom

# --- signatures (64-bit safe: handles must not be truncated to c_int) -------
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetDoubleClickTime.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p

_GA_ROOT = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_WDA_EXCLUDEFROMCAPTURE = 0x11
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20
_ERROR_ACCESS_DENIED = 5
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def enable_dpi_awareness() -> None:
    """Must run before any window exists so all coordinates are physical pixels."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # per-monitor v2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ----------------------------------------------------------------- windows
def foreground_window() -> Optional[int]:
    return user32.GetForegroundWindow() or None


def root_window_at(x: int, y: int) -> Optional[int]:
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return None
    return user32.GetAncestor(hwnd, _GA_ROOT) or hwnd


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_pid_at(x: int, y: int) -> Optional[int]:
    hwnd = root_window_at(x, y)
    return window_pid(hwnd) if hwnd else None


def foreground_is_own() -> bool:
    hwnd = foreground_window()
    return bool(hwnd) and window_pid(hwnd) == OWN_PID


def window_rect(hwnd: int) -> Optional[Rect]:
    """Visible frame of a window (without the invisible resize border/shadow)."""
    r = wintypes.RECT()
    ok = dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
                                      ctypes.byref(r), ctypes.sizeof(r)) == 0
    if not ok and not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_shell_window(hwnd: int) -> bool:
    return window_class(hwnd) in _SHELL_CLASSES


def is_capturable(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd)) and not user32.IsIconic(hwnd) \
        and not is_shell_window(hwnd)


# ------------------------------------------------------------ click config
def double_click_time() -> float:
    return user32.GetDoubleClickTime() / 1000.0


def double_click_slop() -> tuple[int, int]:
    """Half-size (dx, dy) of the rectangle inside which two clicks form a double click."""
    return (max(2, user32.GetSystemMetrics(36) // 2), max(2, user32.GetSystemMetrics(37) // 2))


# ---------------------------------------------------------------- elevation
def process_elevated(pid: int) -> Optional[bool]:
    """True/False, or None when it cannot be determined."""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return True if ctypes.get_last_error() == _ERROR_ACCESS_DENIED else None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            # A normal process cannot open the token of a higher-integrity one.
            return True if ctypes.get_last_error() == _ERROR_ACCESS_DENIED else None
        try:
            elevated = wintypes.DWORD(0)
            size = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(elevated),
                                              ctypes.sizeof(elevated), ctypes.byref(size))
            return bool(elevated.value) if ok else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def is_self_elevated() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def foreground_blocked() -> Optional[str]:
    """Title of the foreground window if it runs elevated while we do not.

    Windows (UIPI) hides mouse/keyboard events and UI Automation data of elevated
    windows from a non-elevated process, so recording them silently fails.
    """
    if is_self_elevated():
        return None
    hwnd = foreground_window()
    if not hwnd:
        return None
    pid = window_pid(hwnd)
    if pid == OWN_PID:
        return None
    if process_elevated(pid):
        return window_title(hwnd) or "an application"
    return None


def relaunch_as_admin() -> bool:
    """Start a new elevated instance (UAC prompt). Caller should quit on True."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, ""
    else:
        exe, params = sys.executable, "-m makemydoc"
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
    return bool(result) and result > 32


# ------------------------------------------------------------------- misc
def exclude_from_capture(widget) -> None:
    """Hide a Tk window from screenshots (Windows 10 2004+; silently ignored otherwise)."""
    try:
        wid = widget.winfo_id()
        hwnd = user32.GetParent(wid) or wid
        user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass
