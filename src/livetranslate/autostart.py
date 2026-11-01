"""Start Live Translate with Windows: a value under the user's Run key in the registry.

It runs when this user signs in; audio devices and the tray only exist from then on.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "LiveTranslate"  # the installer's uninstall section removes this name too


def supported() -> bool:
    return sys.platform == "win32"


def command() -> str:
    """--autostart: the app then shows what the operator chose (window, minimized or tray only)."""
    # The launcher sets LIVETRANSLATE_UV to <install dir>\bin\uv.exe. Going through its
    # script keeps the environment update after a new version working.
    uv = os.environ.get("LIVETRANSLATE_UV")
    script = Path(uv).parent.parent / "LiveTranslate.vbs" if uv else None
    if script is not None and script.exists():
        wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
        return f'"{wscript}" "{script}" --autostart'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" -m livetranslate --autostart'


def apply(enabled: bool, value: str = VALUE) -> bool:
    """Add or remove the Run value; False when that didn't work."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, value, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(key, value)
                except FileNotFoundError:
                    pass
    except OSError:
        log.exception("Could not change the Windows start entry")
        return False
    log.info("Start with Windows: %s", "on" if enabled else "off")
    return True
