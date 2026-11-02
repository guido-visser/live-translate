"""Send translated subtitles to a vMix Title input through vMix's Web API.

vMix: Settings → Web Controller → enable (default port 8088). The API is plain
HTTP GET: /api returns the state as XML, /api/?Function=... performs actions.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx

from ..config import Settings, VmixSettings
from .subtitles import SubtitleOutput, compose, tail  # noqa: F401

log = logging.getLogger(__name__)


def parse_state(xml_text: str) -> dict:
    """Inputs that have text fields, and which input each overlay channel shows."""
    root = ET.fromstring(xml_text)
    inputs = []
    for el in root.iter("input"):
        fields = [t.get("name") for t in el.findall("text") if t.get("name")]
        if not fields:
            continue
        inputs.append(
            {
                "key": el.get("key"),
                "number": int(el.get("number") or 0),
                "title": el.get("title") or el.get("shortTitle") or "",
                "type": el.get("type") or "",
                "fields": fields,
            }
        )
    overlays = {}
    for el in root.iter("overlay"):
        overlays[int(el.get("number") or 0)] = (el.text or "").strip() or None
    version = root.findtext("version") or ""
    return {"version": version, "inputs": inputs, "overlays": overlays}


class VmixClient:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=3, transport=transport)

    async def state(self) -> dict:
        r = await self._client.get(f"{self.url}/api/")
        r.raise_for_status()
        return parse_state(r.text)

    async def call(self, function: str, **params: str) -> None:
        r = await self._client.get(f"{self.url}/api/", params={"Function": function, **params})
        r.raise_for_status()

    async def set_text(self, input_key: str, field: str, text: str) -> None:
        await self.call("SetText", Input=input_key, SelectedName=field, Value=text)

    async def overlay(self, channel: int, on: bool, input_key: str) -> None:
        if on:
            await self.call(f"OverlayInput{channel}In", Input=input_key)
        else:
            await self.call(f"OverlayInput{channel}Out")

    async def close(self) -> None:
        await self._client.aclose()


class VmixOutput(SubtitleOutput):
    name = "vMix"
    errors = (httpx.HTTPError, ET.ParseError)

    def __init__(self, get_settings: Callable[[], Settings], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self._get_settings = get_settings
        self._transport = transport
        self._client: VmixClient | None = None

    @property
    def settings(self) -> VmixSettings:
        return self._get_settings().vmix

    @property
    def ready(self) -> bool:
        s = self.settings
        return bool(s.input and s.field)

    def client(self) -> VmixClient:
        if self._client is None or self._client.url != self.settings.url.rstrip("/"):
            self._client = VmixClient(self.settings.url, self._transport)
        return self._client

    async def _set_text(self, text: str) -> None:
        await self.client().set_text(self.settings.input, self.settings.field, text)

    async def _set_overlay(self, on: bool) -> None:
        await self.client().overlay(self.settings.overlay, on, self.settings.input)

    async def state(self) -> dict | None:
        result: dict = {}

        async def fetch():
            result.update(await self.client().state())

        return result if await self._guard(fetch()) else None

    async def refresh(self) -> dict:
        return self.status

    async def overlay(self, on: bool) -> bool:
        if not self.settings.input:  # the overlay only needs the input, not the text field
            return False
        ok = await self._guard(self._set_overlay(on))
        if ok:
            self.status["overlay"] = on
        return ok

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
