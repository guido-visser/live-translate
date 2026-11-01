"""The operator page in a window of its own, instead of a browser tab.

A small native window around the Edge engine Windows already has (WebView2, through
pywebview). It runs as its own process: the tray icon also wants the GUI thread, and a
crash in there must not take the translation down. When it can't start, the page opens
in the browser as before. Started by the app:
python -m livetranslate.window <url> <title> [--minimized]
"""

from __future__ import annotations

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
    import ctypes

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
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW(None, False, f"Local\\LiveTranslate-Window-{title}")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _set_icon(title: str, data: Path) -> None:
    """Without this the window and its taskbar button show the Python icon."""
    import ctypes

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
    import ctypes

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
    window = webview.create_window(
        title,
        url,
        width=1180,
        height=860,
        min_size=(420, 500),
        text_select=True,
        minimized="--minimized" in sys.argv,
    )
    window.events.shown += lambda: _set_icon(title, data)
    threading.Thread(target=_close_with_the_app, args=(window, url), daemon=True).start()
    # Not private: the pages remember the interface language and panels in localStorage.
    webview.start(private_mode=False, storage_path=str(data / "webview"))
    return 0


if __name__ == "__main__":
    from .__main__ import ensure_streams

    ensure_streams()  # no console under pythonw
    sys.exit(main())
