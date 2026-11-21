"""LAN address detection, QR code and GPU statistics."""

from __future__ import annotations

import io
import ipaddress
import logging
import re
import socket
import subprocess
import sys
import time

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


# Adapters phones can never reach: Hyper-V/WSL switches, virtual machines, VPNs.
_VIRTUAL = ("hyper-v", "virtual", "vmware", "virtualbox", "wsl", "vpn", "tap-", "wintun", "wireguard", "zerotier", "tailscale")


def _windows_networks() -> list[tuple[str, str, int]]:
    """(adapter name, IPv4 address, IfType) of every connected network adapter, from GetAdaptersAddresses."""
    import ctypes
    from ctypes import wintypes

    class SockAddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("port", ctypes.c_ushort), ("addr", ctypes.c_ubyte * 4)]

    class SocketAddress(ctypes.Structure):
        _fields_ = [("sockaddr", ctypes.POINTER(SockAddr)), ("length", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        pass

    Unicast._fields_ = [("header", ctypes.c_ulonglong), ("next", ctypes.POINTER(Unicast)), ("address", SocketAddress)]

    class Adapter(ctypes.Structure):
        pass

    Adapter._fields_ = [
        ("header", ctypes.c_ulonglong),
        ("next", ctypes.POINTER(Adapter)),
        ("adapter_name", ctypes.c_char_p),
        ("first_unicast", ctypes.POINTER(Unicast)),
        ("first_anycast", ctypes.c_void_p),
        ("first_multicast", ctypes.c_void_p),
        ("first_dns", ctypes.c_void_p),
        ("dns_suffix", wintypes.LPWSTR),
        ("description", wintypes.LPWSTR),
        ("friendly_name", wintypes.LPWSTR),
        ("physical_address", ctypes.c_ubyte * 8),
        ("physical_address_length", wintypes.ULONG),
        ("flags", wintypes.ULONG),
        ("mtu", wintypes.ULONG),
        ("if_type", wintypes.ULONG),
        ("oper_status", ctypes.c_int),
    ]

    get = ctypes.windll.iphlpapi.GetAdaptersAddresses
    flags = 0x2 | 0x4 | 0x8  # skip anycast, multicast and DNS server addresses
    size = wintypes.ULONG(16384)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        err = get(socket.AF_INET, flags, None, buf, ctypes.byref(size))
        if err != 111:  # ERROR_BUFFER_OVERFLOW: size now holds what is needed
            break
    if err != 0:
        raise OSError(err, "GetAdaptersAddresses failed")
    found: list[tuple[str, str, int]] = []
    adapter = ctypes.cast(buf, ctypes.POINTER(Adapter))
    while adapter:
        a = adapter.contents
        name, description = a.friendly_name or "", a.description or ""
        virtual = any(v in f"{name} {description}".lower() for v in _VIRTUAL) or name.lower().startswith("vethernet")
        if a.oper_status == 1 and a.if_type != 24 and not virtual:  # up, not loopback
            unicast = a.first_unicast
            while unicast:
                sa = unicast.contents.address.sockaddr
                if sa and sa.contents.family == socket.AF_INET:
                    found.append((name, ".".join(str(b) for b in sa.contents.addr), a.if_type))
                unicast = unicast.contents.next
        adapter = a.next
    return found


_WIFI = 71  # IfType of a Wi-Fi adapter
_ssid_cache: tuple[float, dict[str, str]] = (0.0, {})


def _wifi_ssids() -> dict[str, str]:
    """{adapter name: SSID} of the Wi-Fi networks this PC itself is connected to (netsh, cached briefly)."""
    global _ssid_cache
    if time.monotonic() - _ssid_cache[0] < 10:
        return _ssid_cache[1]
    out = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"],
        capture_output=True, encoding="oem", errors="replace", timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    ssids: dict[str, str] = {}
    # One block per adapter; its first line is the (translated) "Name : WiFi", "SSID" is never translated.
    for block in re.split(r"\n\s*\n", out):
        pairs = [(k.strip(), v.strip()) for k, _, v in (line.partition(":") for line in block.splitlines()) if v.strip()]
        ssid = next((v for k, v in pairs if k == "SSID"), "")
        if pairs and ssid:
            ssids[pairs[0][1]] = ssid
    _ssid_cache = (time.monotonic(), ssids)
    return ssids


def subnet(ip: str) -> str:
    """What a network is remembered by: "192.168.10" (the same after the router hands out another address)."""
    return ip.rsplit(".", 1)[0]


def lan_networks() -> list[dict]:
    """Every network a phone could reach this PC on:
    [{"name": "Ethernet 2", "ip": "192.168.10.212", "ssid": ""}, {"name": "WiFi", ..., "ssid": "Church"}].

    The one with the default route (usually the one with internet) comes first. Virtual adapters
    are left out. Without adapter names (not Windows) the name is empty. "ssid" is only known for
    a Wi-Fi network this PC is on itself, not for a router it is cabled to."""
    addresses = lan_addresses()
    named: list[tuple[str, str, int]] = []
    if sys.platform == "win32":
        try:
            named = [n for n in _windows_networks() if _is_lan(n[1])]
        except Exception:
            log.debug("Could not list the network adapters", exc_info=True)
    if not named:
        return [{"name": "", "ip": ip, "ssid": ""} for ip in addresses]
    ssids: dict[str, str] = {}
    if any(t == _WIFI for _, _, t in named):
        try:
            ssids = _wifi_ssids()
        except Exception:
            log.debug("Could not read the Wi-Fi name", exc_info=True)
    order = {ip: i for i, ip in enumerate(addresses)}
    named.sort(key=lambda n: order.get(n[1], len(order)))
    return [{"name": n, "ip": ip, "ssid": ssids.get(n, "") if t == _WIFI else ""} for n, ip, t in named]


def host_url(host: str, port: int) -> str:
    if "://" in host:
        return host.rstrip("/") + "/"
    return f"http://{host}/" if port == 80 else f"http://{host}:{port}/"


def listener_url(public_host: str, port: int) -> str:
    return host_url(public_host.strip() or next(iter(lan_addresses()), "127.0.0.1"), port)


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
