"""Enumerate audio inputs: capture devices and (on Windows) WASAPI loopback outputs."""

from __future__ import annotations

import logging
import sys
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    id: str  # stable "<host api>::<name>" so a saved choice survives reboots
    name: str
    host_api: str
    channels: int
    sample_rate: int
    kind: str  # "device" | "loopback"
    is_default: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _device_id(host_api: str, name: str) -> str:
    return f"{host_api}::{name}"


def list_input_devices() -> list[DeviceInfo]:
    import sounddevice as sd

    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    host_apis = sd.query_hostapis()
    devices = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        host_api = host_apis[dev["hostapi"]]["name"]
        # Kernel-streaming duplicates are rarely usable and clutter the list.
        if host_api == "Windows WDM-KS":
            continue
        devices.append(
            DeviceInfo(
                id=_device_id(host_api, dev["name"]),
                name=dev["name"],
                host_api=host_api,
                channels=int(dev["max_input_channels"]),
                sample_rate=int(dev["default_samplerate"]),
                kind="device",
                is_default=index == default_in,
            )
        )
    return devices


def resolve_input_device(device_id: str | None) -> int | None:
    """Map a stable device id back to a PortAudio index (None = default)."""
    if not device_id:
        return None
    import sounddevice as sd

    host_apis = sd.query_hostapis()
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        if _device_id(host_apis[dev["hostapi"]]["name"], dev["name"]) == device_id:
            return index
    raise LookupError(f"Audio device not found: {device_id}")


def same_device_other_apis(device_id: str | None) -> list[int]:
    """The same physical device under other host APIs, to try when the chosen one won't open.

    Some USB drivers fail in WASAPI but work through MME/DirectSound. MME cuts names
    at 31 characters, so names are compared by prefix.
    """
    if not device_id:
        return []
    import sounddevice as sd

    name = device_id.split("::", 1)[1].strip()
    host_apis = sd.query_hostapis()
    preference = {"Windows DirectSound": 0, "MME": 1, "Windows WASAPI": 2}
    found = []
    for index, dev in enumerate(sd.query_devices()):
        api = host_apis[dev["hostapi"]]["name"]
        other = dev["name"].strip()
        if dev["max_input_channels"] <= 0 or _device_id(api, dev["name"]) == device_id or api == "Windows WDM-KS":
            continue
        if other and (name.startswith(other) or other.startswith(name)):
            found.append((preference.get(api, 3), index))
    return [index for _, index in sorted(found)]


def list_loopback_devices() -> list[DeviceInfo]:
    """Playback devices that can be recorded ("what you hear"). Windows only."""
    if sys.platform != "win32":
        return []
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    p = pyaudio.PyAudio()
    try:
        try:
            default_name = p.get_default_wasapi_loopback()["name"]
        except Exception:
            default_name = None
        return [
            DeviceInfo(
                id=_device_id("WASAPI loopback", dev["name"]),
                name=dev["name"],
                host_api="WASAPI loopback",
                channels=int(dev["maxInputChannels"]),
                sample_rate=int(dev["defaultSampleRate"]),
                kind="loopback",
                is_default=dev["name"] == default_name,
            )
            for dev in p.get_loopback_device_info_generator()
        ]
    finally:
        p.terminate()


def list_all() -> list[dict]:
    devices: list[DeviceInfo] = []
    try:
        devices += list_input_devices()
    except Exception:
        log.exception("Listing input devices failed")
    try:
        devices += list_loopback_devices()
    except Exception:
        log.exception("Listing loopback devices failed")
    return [d.to_dict() for d in devices]
