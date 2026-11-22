"""The Windows Firewall rule that lets phones reach the web server ("Live Translate").

The installer makes it for port 8000. When the port is changed in Settings → Web Server, the app
updates the rule itself; that needs administrator rights, so Windows asks once (UAC). Reading the
rule doesn't: Get-NetFirewallRule works for any user, and unlike netsh its output doesn't depend on
the language of Windows.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

RULE = "Live Translate"
CACHE_S = 30
_CREATE_NO_WINDOW = 0x08000000

_lock = threading.Lock()
_cache: tuple[float, list[str] | None] | None = None


def _powershell(command: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def parse_ports(output: str) -> list[str]:
    """LocalPort of each port filter: "8000", "Any", "8000-8010" or "80,8000" (one per line)."""
    ports: list[str] = []
    for line in output.splitlines():
        for part in line.split(","):
            if part.strip():
                ports.append(part.strip())
    return ports


def rule_ports(fresh: bool = False) -> list[str] | None:
    """The ports the rule opens: [] when there is no rule, None when it can't be read."""
    global _cache
    if sys.platform != "win32":
        return None
    with _lock:
        if not fresh and _cache and time.monotonic() - _cache[0] < CACHE_S:
            return _cache[1]
        ports: list[str] | None
        try:
            done = _powershell(
                # No rule is an ObjectNotFound error: that is an answer ([]), any other error isn't.
                f"Get-NetFirewallRule -DisplayName '{RULE}' -ErrorAction SilentlyContinue -ErrorVariable e"
                " | Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' }"
                " | Get-NetFirewallPortFilter | Select-Object -ExpandProperty LocalPort;"
                " if ($e -and $e[0].CategoryInfo.Category -ne 'ObjectNotFound') { exit 1 }; exit 0"
            )
            ports = parse_ports(done.stdout) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            log.warning("Could not read the firewall rule", exc_info=True)
            ports = None
        _cache = (time.monotonic(), ports)
        return ports


def opens(ports: list[str], port: int) -> bool:
    for p in ports:
        if p.lower() == "any":
            return True
        low, _, high = p.partition("-")
        try:
            if int(low) <= port <= int(high or low):
                return True
        except ValueError:
            continue
    return False


def status(port: int, fresh: bool = False) -> str:
    """"ok", "missing" (no rule), "mismatch" (a rule for another port) or "unknown"."""
    ports = rule_ports(fresh)
    if ports is None:
        return "unknown"
    if not ports:
        return "missing"
    return "ok" if opens(ports, port) else "mismatch"


def update_rule(port: int) -> str:
    """Make the rule open this port; Windows asks for administrator rights. Blocks until done.

    Returns "ok", "cancelled" (UAC declined) or "failed"."""
    global _cache
    if sys.platform != "win32":
        return "failed"
    import ctypes
    from ctypes import wintypes

    port = int(port)  # it goes into a command line
    command = (
        f"Remove-NetFirewallRule -DisplayName '{RULE}' -ErrorAction SilentlyContinue; "
        f"New-NetFirewallRule -DisplayName '{RULE}' -Direction Inbound -Action Allow -Protocol TCP "
        f"-LocalPort {port} -Profile Any | Out-Null"
    )

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000
    ERROR_CANCELLED = 1223
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f'-NoProfile -NonInteractive -WindowStyle Hidden -Command "{command}"'
    info.nShow = 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            log.info("Firewall rule not changed: administrator rights were declined")
            return "cancelled"
        log.warning("Could not start the firewall change (error %d)", error)
        return "failed"
    try:
        kernel32.WaitForSingleObject(info.hProcess, 60_000)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        kernel32.CloseHandle(info.hProcess)
    with _lock:
        _cache = None
    if status(port, fresh=True) == "ok":
        log.info("Firewall rule now opens port %d", port)
        return "ok"
    log.warning("The firewall change for port %d did not work (exit code %d)", port, code.value)
    return "failed"
