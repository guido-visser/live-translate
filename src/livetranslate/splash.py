"""A small "Live Translate is starting" window while the app loads.

On a busy PC (Windows updates, Defender scanning after a wake from hibernation) the start can
take minutes. Without anything on screen it looked like nothing happened, and it was clicked
again and again. The app starts this as its own process so it keeps responding while the app
loads its models; it closes when the app is ready, or when the app that started it ends.
python -m livetranslate.splash <pid of the app>
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

TITLE = "Live Translate "  # the trailing space keeps it apart from the operator window's title

TEXTS = {
    "nl": ("Live Translate wordt gestart…", "Na een Windows-update of als de computer net aan staat kan dit een paar minuten duren."),
    "en": ("Live Translate is starting…", "After a Windows update, or right after the computer starts, this can take a few minutes."),
    "ru": ("Live Translate запускается…", "После обновления Windows или сразу после включения компьютера это может занять несколько минут."),
    "uk": ("Live Translate запускається…", "Після оновлення Windows або одразу після ввімкнення комп'ютера це може тривати кілька хвилин."),
}


def show(parent_pid: int) -> subprocess.Popen | None:
    """Start the window for this process; None where there is none (not Windows, no tkinter)."""
    if sys.platform != "win32":
        return None
    try:
        return subprocess.Popen([sys.executable, "-m", "livetranslate.splash", str(parent_pid)])
    except OSError:
        return None


def close(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass


def _alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _already_shown() -> bool:
    """One splash at a time: a second click brings the one that's there to the front."""
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return False
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    return True


def main() -> int:
    parent = int(sys.argv[1])
    if _already_shown():
        return 0
    import tkinter as tk
    from pathlib import Path
    from tkinter import ttk

    from .languages import ui_language

    heading, detail = TEXTS.get(ui_language(), TEXTS["en"])
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LiveTranslate.Splash")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

    root = tk.Tk()
    root.title(TITLE)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    try:
        root.iconbitmap(str(Path(__file__).parent / "web" / "icon.ico"))
    except tk.TclError:
        pass
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=heading, font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frame, text=detail, font=("Segoe UI", 10), wraplength=380, justify="left").pack(anchor="w", pady=(8, 16))
    bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
    bar.pack(fill="x")
    bar.start(15)

    root.update_idletasks()
    width, height = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.after(3000, lambda: root.attributes("-topmost", False))  # on top when it appears, not forever

    def watch() -> None:
        if _alive(parent):
            root.after(1000, watch)
        else:
            root.destroy()

    watch()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
