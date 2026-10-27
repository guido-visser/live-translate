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

log = logging.getLogger(__name__)


def tail(text: str, max_chars: int) -> str:
    """The end of a long sentence, cut at a word boundary, marked with an ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[-(max_chars - 1) :]
    space = cut.find(" ")
    if 0 <= space < len(cut) - 1:
        cut = cut[space + 1 :]
    return "…" + cut


def compose(previous: str | None, current: str, max_chars: int) -> str:
    """Rolling two-line subtitle: previous sentence above the current one, if both fit."""
    current = tail(current, max_chars)
    if previous and len(previous) + 1 + len(current) <= max_chars:
        return f"{previous}\n{current}"
    return current


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


class VmixOutput:
    """Rolling subtitles + overlay control. Failures are reported, never raised."""

    def __init__(self, get_settings: Callable[[], Settings], transport: httpx.AsyncBaseTransport | None = None):
        self._get_settings = get_settings
        self._transport = transport
        self._client: VmixClient | None = None
        self.previous: str | None = None
        self.status = {"ok": None, "error": "", "last_text": "", "overlay": None}

    @property
    def settings(self) -> VmixSettings:
        return self._get_settings().vmix

    @property
    def active(self) -> bool:
        s = self.settings
        return s.enabled and bool(s.input and s.field)

    def client(self) -> VmixClient:
        if self._client is None or self._client.url != self.settings.url.rstrip("/"):
            self._client = VmixClient(self.settings.url, self._transport)
        return self._client

    async def _guard(self, action) -> bool:
        try:
            await action
        except (httpx.HTTPError, ET.ParseError) as exc:
            message = str(exc) or exc.__class__.__name__
            if self.status["error"] != message:
                log.warning("vMix: %s", message)
            self.status.update(ok=False, error=message)
            return False
        self.status.update(ok=True, error="")
        return True

    async def inputs(self) -> dict | None:
        result: dict = {}

        async def fetch():
            result.update(await self.client().state())

        return result if await self._guard(fetch()) else None

    async def show(self, text: str) -> bool:
        s = self.settings
        if not (s.input and s.field):
            return False
        ok = await self._guard(self.client().set_text(s.input, s.field, text))
        if ok:
            self.status["last_text"] = text
        return ok

    async def push(self, sentence: str) -> None:
        if not self.active:
            return
        text = compose(self.previous, sentence, self.settings.max_chars)
        self.previous = text.split("\n")[-1]
        await self.show(text)

    async def clear(self) -> None:
        self.previous = None
        if self.active:
            await self.show("")

    async def overlay(self, on: bool) -> bool:
        s = self.settings
        if not s.input:
            return False
        ok = await self._guard(self.client().overlay(s.overlay, on, s.input))
        if ok:
            self.status["overlay"] = on
        return ok

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
