"""Check that native libraries load before the app imports them in its own process.

onnxruntime (voice detection, Piper voices) and sentencepiece are built with a recent
MSVC and crash the whole process with an access violation on import when Windows has
an older Microsoft Visual C++ runtime (below 14.40). A crash like that can't be caught,
so the import is tried once in a separate process first; when it fails, speech
recognition and spoken translations refuse to start with a message that says how to
fix it, and the rest of the app keeps running.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
MIN_VC_RUNTIME = (14, 40)
# Exit codes of a process killed by an access violation (0xC0000005), signed and unsigned.
ACCESS_VIOLATION = (-1073741819, 3221225477)

_lock = threading.Lock()
_onnx_error: str | None = None  # None: not checked yet; "": works


def vc_runtime_version() -> tuple[int, ...] | None:
    """Version of the Visual C++ runtime in System32 (msvcp140.dll), or None when unknown."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        dll = Path(_system_dir()) / "msvcp140.dll"
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(dll), None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(dll), 0, size, data):
            return None
        info = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(length)):
            return None
        # VS_FIXEDFILEINFO: signature, struct version, then the file version (MS, LS).
        ms, ls = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD))[2:4]
        return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        log.debug("Could not read the Visual C++ runtime version", exc_info=True)
        return None


def _system_dir() -> str:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    return buf.value


def vc_runtime_outdated() -> bool:
    version = vc_runtime_version()
    return version is not None and version[:2] < MIN_VC_RUNTIME


def runtime_hint() -> str:
    """What to do when a native library fails to load; empty when the runtime looks fine."""
    version = vc_runtime_version()
    if version is None or version[:2] >= MIN_VC_RUNTIME:
        return ""
    have = ".".join(map(str, version[:3]))
    need = ".".join(map(str, MIN_VC_RUNTIME))
    return (
        f"The Microsoft Visual C++ runtime on this PC is too old ({have}, needs {need} or newer). "
        f"Install the latest one from {VC_REDIST_URL} (or reinstall Live Translate) and restart the app."
    )


def _probe(module: str) -> str:
    """Import a module in a separate Python process; returns "" or what went wrong."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            creationflags=flags,
        )
    except Exception as exc:
        log.warning("Could not check %s: %s", module, exc)
        return ""  # can't tell; let the real import decide
    if proc.returncode == 0:
        return ""
    if proc.returncode in ACCESS_VIOLATION:
        detail = "crashed with an access violation"
    else:
        lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
        detail = lines[-1].strip() if lines else f"exit code {proc.returncode}"
    message = f"{module} cannot be loaded ({detail})."
    hint = runtime_hint()
    return f"{message} {hint}" if hint else message


def onnxruntime_error() -> str:
    """Checks once (blocking) whether onnxruntime loads; returns "" or the problem."""
    global _onnx_error
    with _lock:
        if _onnx_error is None:
            _onnx_error = _probe("onnxruntime")
            if _onnx_error:
                log.error("%s Speech recognition and spoken translations are disabled.", _onnx_error)
        return _onnx_error


def onnxruntime_failed() -> bool:
    """True when the check already ran and failed (doesn't run it)."""
    return bool(_onnx_error)


def require_onnxruntime() -> None:
    """Raise RuntimeError instead of letting the import crash the process."""
    error = onnxruntime_error()
    if error:
        raise RuntimeError(error)
