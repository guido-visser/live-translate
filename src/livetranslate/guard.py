"""Starts Live Translate again when it crashes.

A crash in native code (a GPU driver, a DLL) ends the process at once; Python can't catch it.
On 13 Nov 2026 that left the church without translation until someone noticed. This small
process waits next to the app and, when the app ends with a crash code, starts it again (and
lets it go on translating when it was). It uses only the standard library, so it can't crash
the same way. Normal ends (Exit in the tray, an update, the app restarting itself, Task Manager,
Windows shutting down) leave it alone.

    pythonw -m livetranslate.guard --pid 1234 --data <data dir> -- <the app's own arguments>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Restarts allowed within the window; past that a person has to look (it would only crash again).
MAX_CRASH_RESTARTS = 3
WINDOW_S = 30 * 60
HISTORY_FILE = "crash-restarts.json"
# Exists while the app translates (see pipeline.py); then the restarted app goes on translating.
TRANSLATING_FILE = "translating"

STATUS_CONTROL_C_EXIT = 0xC000013A  # console closed, logoff: not a crash
ABORT = 3  # Python's fatal errors and C abort()


def is_crash(code: int) -> bool:
    """Exit codes of a crash: NTSTATUS errors (access violation, heap corruption, fail-fast...) and abort.

    Not 0 (normal exit, self-restart, update) and not 1 (Task Manager, an ordinary Python error
    on the way out)."""
    code &= 0xFFFFFFFF
    if code == STATUS_CONTROL_C_EXIT:
        return False
    return code >= 0xC0000000 or code == ABORT


def allowed(history: list[float], now: float) -> bool:
    return len([t for t in history if now - t < WINDOW_S]) < MAX_CRASH_RESTARTS


def _load_history(path: Path) -> list[float]:
    try:
        return [float(t) for t in json.loads(path.read_text("utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def _write_log(data: Path, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')},000 {'ERROR':<7} livetranslate.guard: {message}\n"
    for name in ("livetranslate.log", "crash.log"):
        try:
            with open(data / name, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def _wait_for_exit(pid: int) -> int | None:
    """The process's exit code once it ends; None when it can't be watched."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value
    finally:
        kernel32.CloseHandle(handle)


def _shutting_down() -> bool:
    import ctypes

    return bool(ctypes.windll.user32.GetSystemMetrics(0x2000))  # SM_SHUTTINGDOWN


def restart(data: Path, pid: int, code: int, app_args: list[str]) -> bool:
    """Start the app again after a crash, unless it crashed too often lately. True when started."""
    from .__main__ import restart_command  # only definitions; nothing heavy is imported

    history_path = data / HISTORY_FILE
    now = time.time()
    history = [t for t in _load_history(history_path) if now - t < WINDOW_S]
    if not allowed(history, now):
        _write_log(
            data,
            f"Live Translate crashed (exit code 0x{code:08X}) and has crashed {len(history)} times in "
            f"{WINDOW_S // 60} minutes; not starting it again. Start it by hand.",
        )
        return False
    resume = (data / TRANSLATING_FILE).exists()
    _write_log(
        data,
        f"Live Translate crashed (exit code 0x{code:08X}); starting it again"
        + (" and translating on" if resume else ""),
    )
    try:
        history_path.write_text(json.dumps([*history, now]), "utf-8")
    except OSError:
        pass
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(restart_command(app_args, pid, resume), creationflags=flags, close_fds=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="livetranslate.guard")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("app_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    app_args = args.app_args[1:] if args.app_args[:1] == ["--"] else args.app_args
    if sys.platform != "win32":
        return 0
    code = _wait_for_exit(args.pid)
    if code is None or not is_crash(code) or _shutting_down():
        return 0
    try:
        restart(args.data, args.pid, code, app_args)
    except Exception as exc:  # nothing to show it to; the log is all there is
        _write_log(args.data, f"Could not start Live Translate again: {exc!r}")
    return 0


def start(data: Path, app_args: list[str]) -> None:
    """Start a guard for this process (Windows; best effort)."""
    if sys.platform != "win32":
        return
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # no console window, also when the app runs in one
    command = [str(pythonw if pythonw.exists() else exe), "-m", "livetranslate.guard"]
    command += ["--pid", str(os.getpid()), "--data", str(data), "--", *app_args]
    subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


if __name__ == "__main__":
    sys.exit(main())
