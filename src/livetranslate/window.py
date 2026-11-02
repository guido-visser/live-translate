"""The operator page in a window of its own, instead of a browser tab.

A small native window around the Edge engine Windows already has (WebView2, through
pywebview). It runs as its own process: the tray icon also wants the GUI thread, and a
crash in there must not take the translation down. When it can't start, the page opens
in the browser as before. Started by the app:
python -m livetranslate.window <url> <title> [--minimized]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

TITLE = "Live Translate"
ICON = Path(__file__).parent / "web" / "icon.png"
STARTUP_S = 5  # a window process that ends this fast didn't get a window up
SERVER_CHECK_S = 5
WIDTH, HEIGHT = 1180, 860  # until the window has been closed once
MIN_SIZE = (420, 500)
SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED = 2, 3
WPF_RESTORETOMAXIMIZED = 2


def open_page(url: str, use_window: bool = True, title: str = TITLE, minimized: bool = False) -> None:
    """Show a page of the app. Blocks for a few seconds, to notice a window that can't start."""
    if not use_window or sys.platform != "win32":
        webbrowser.open(url)
        return
    command = [sys.executable, "-m", "livetranslate.window", url, title]
    try:
        process = subprocess.Popen([*command, "--minimized"] if minimized else command)
        if process.wait(timeout=STARTUP_S) == 0:
            return  # the window was already open and came to the front
        log.warning("The app window could not start (exit code %s)", process.returncode)
    except subprocess.TimeoutExpired:
        return  # it's up
    except OSError:
        log.warning("The app window could not start", exc_info=True)
    webbrowser.open(url)


# -- the window process --------------------------------------------------------


def _bring_to_front(title: str) -> None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        hwnd = ctypes.c_void_p(hwnd)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _claim(title: str) -> bool:
    """One window per title; False when it's already open. The handle lasts as long as the process."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW(None, False, f"Local\\LiveTranslate-Window-{title}")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _set_icon(title: str, data: Path) -> None:
    """Without this the window and its taskbar button show the Python icon."""
    from PIL import Image

    path = data / "window.ico"
    if not path.exists():
        Image.open(ICON).save(path, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    hwnd = user32.FindWindowW(None, title)
    for which, size in ((0, 16), (1, 32)):  # WM_SETICON: ICON_SMALL, ICON_BIG
        icon = user32.LoadImageW(None, str(path), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        user32.SendMessageW(hwnd, 0x80, which, icon)


def load_state(path: Path, title: str) -> dict:
    """The size this window had when it last closed (in the units pywebview takes), and whether it was maximized."""
    state = {"width": WIDTH, "height": HEIGHT, "maximized": False}
    try:
        saved = json.loads(path.read_text("utf-8"))[title]
        state.update(width=int(saved["width"]), height=int(saved["height"]), maximized=bool(saved["maximized"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # never opened yet, or a damaged file: the default is fine
    return state


def save_state(path: Path, title: str, state: dict) -> None:
    try:
        saved = json.loads(path.read_text("utf-8"))
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    saved[title] = state
    path.write_text(json.dumps(saved), "utf-8")


def centered(width: int, height: int, area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Position and size in the center of (left, top, width, height). The size is the saved one if
    it still fits: the screen can have changed."""
    left, top, area_width, area_height = area
    width, height = max(MIN_SIZE[0], min(width, area_width)), max(MIN_SIZE[1], min(height, area_height))
    return (left + (area_width - width) // 2, top + (area_height - height) // 2, width, height)


class _Placement(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", ctypes.wintypes.POINT),
        ("ptMaxPosition", ctypes.wintypes.POINT),
        ("rcNormalPosition", ctypes.wintypes.RECT),
    ]


def _placement(title: str) -> tuple[ctypes.c_void_p, _Placement] | None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return None
    hwnd = ctypes.c_void_p(hwnd)
    placement = _Placement(length=ctypes.sizeof(_Placement))
    return (hwnd, placement) if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)) else None


def _work_area() -> tuple[int, int, int, int]:
    """The main screen without the taskbar, in the units pywebview takes."""
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()  # pywebview does this too; before it these numbers would depend on the order
    area = ctypes.wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
    scale = user32.GetDpiForSystem() / 96
    return (
        int(area.left / scale),
        int(area.top / scale),
        int((area.right - area.left) / scale),
        int((area.bottom - area.top) / scale),
    )


def _remember(path: Path, title: str) -> None:
    """Stores the size it has when not maximized, so un-maximizing later still gives that size."""
    try:
        found = _placement(title)
        if found is None:
            return
        hwnd, placement = found
        normal = placement.rcNormalPosition
        scale = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96
        minimized_from_maximized = placement.showCmd == SW_SHOWMINIMIZED and placement.flags & WPF_RESTORETOMAXIMIZED
        state = {
            "width": round((normal.right - normal.left) / scale),
            "height": round((normal.bottom - normal.top) / scale),
            "maximized": placement.showCmd == SW_SHOWMAXIMIZED or bool(minimized_from_maximized),
        }
        save_state(path, title, state)
    except Exception:  # closing the window matters more than remembering its size
        log.warning("Could not remember the window size", exc_info=True)


def _restore_to_maximized(title: str) -> None:
    """Started minimized after being maximized: stay on the taskbar, but open up maximized."""
    found = _placement(title)
    if found is not None:
        hwnd, placement = found
        placement.flags |= WPF_RESTORETOMAXIMIZED
        ctypes.windll.user32.SetWindowPlacement(hwnd, ctypes.byref(placement))


def _close_with_the_app(window, url: str) -> None:
    """The app is gone (Exit in the tray, an update): don't leave a dead page on the screen."""
    parts = urlsplit(url)
    probe = f"{parts.scheme}://{parts.netloc}/api/public"
    failures = 0
    while failures < 2:
        time.sleep(SERVER_CHECK_S)
        try:
            urllib.request.urlopen(probe, timeout=3).close()
            failures = 0
        except (urllib.error.URLError, OSError):
            failures += 1
    window.destroy()


def main() -> int:
    url, title = sys.argv[1], sys.argv[2]
    if not _claim(title):
        _bring_to_front(title)
        return 0

    import webview

    from .config import data_dir

    # Its own taskbar button, not grouped with other Python programs.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LiveTranslate.Operator")
    data = data_dir()
    webview.settings["ALLOW_DOWNLOADS"] = True  # the church profile
    state_file = data / "window.json"
    state = load_state(state_file, title)
    minimized = "--minimized" in sys.argv
    x, y, width, height = centered(state["width"], state["height"], _work_area())
    window = webview.create_window(
        title,
        url,
        x=x,  # without a position Windows places it, not in the center
        y=y,
        width=width,
        height=height,
        min_size=MIN_SIZE,
        text_select=True,
        minimized=minimized,
        maximized=state["maximized"] and not minimized,  # pywebview would ignore minimized
    )

    def on_shown() -> None:
        _set_icon(title, data)
        if minimized and state["maximized"]:
            _restore_to_maximized(title)

    window.events.shown += on_shown
    window.events.closing += lambda: _remember(state_file, title)
    threading.Thread(target=_close_with_the_app, args=(window, url), daemon=True).start()
    # Not private: the pages remember the interface language and panels in localStorage.
    webview.start(private_mode=False, storage_path=str(data / "webview"))
    return 0


if __name__ == "__main__":
    from .__main__ import ensure_streams

    ensure_streams()  # no console under pythonw
    sys.exit(main())
