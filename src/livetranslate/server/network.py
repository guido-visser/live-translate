"""LAN address detection, QR code and GPU statistics."""

from __future__ import annotations

import io
import ipaddress
import logging
import socket

import segno

log = logging.getLogger(__name__)


def _is_lan(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_private and not addr.is_loopback and not addr.is_link_local


def lan_addresses() -> list[str]:
    """All private IPv4 addresses of this PC, the one with the default route first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets are sent
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    result: list[str] = []
    for ip in found:
        if _is_lan(ip) and ip not in result:
            result.append(ip)
    return result


def listener_url(public_host: str, port: int) -> str:
    host = public_host.strip() or next(iter(lan_addresses()), "127.0.0.1")
    if "://" in host:
        return host.rstrip("/") + "/"
    return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"


def qr_svg(url: str) -> str:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff", xmldecl=False)
    return buf.getvalue().decode()


class GpuStats:
    def __init__(self) -> None:
        self._handle = None
        self._failed = False

    def read(self) -> dict | None:
        if self._failed:
            return None
        try:
            import pynvml

            if self._handle is None:
                pynvml.nvmlInit()
                self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(self._handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            return {
                "util": util.gpu,
                "mem_used_mb": mem.used // 2**20,
                "mem_total_mb": mem.total // 2**20,
            }
        except Exception:
            self._failed = True
            return None
