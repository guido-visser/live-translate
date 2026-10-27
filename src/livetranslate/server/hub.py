"""WebSocket connections: listeners (phones) grouped by language, and admin pages."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter

from fastapi import WebSocket

log = logging.getLogger(__name__)


class Connection:
    """Each connection gets its own send queue so slow phones never block the pipeline."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.lang: str | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._writer = asyncio.create_task(self._write())

    def send(self, msg: dict) -> None:
        try:
            self._queue.put_nowait(json.dumps(msg, ensure_ascii=False))
        except asyncio.QueueFull:
            log.warning("Dropping message for a slow client")

    async def _write(self) -> None:
        try:
            while True:
                await self.ws.send_text(await self._queue.get())
        except Exception:
            pass  # connection closed; the receive loop cleans up

    def close(self) -> None:
        self._writer.cancel()


class Hub:
    def __init__(self) -> None:
        self.listeners: set[Connection] = set()
        self.admins: set[Connection] = set()
        self._last_typing = 0.0

    def counts(self) -> dict[str, int]:
        return dict(Counter(c.lang for c in self.listeners if c.lang))

    def active_languages(self) -> set[str]:
        return {c.lang for c in self.listeners if c.lang}

    def to_language(self, lang: str, msg: dict) -> None:
        for conn in self.listeners:
            if conn.lang == lang:
                conn.send(msg)

    def to_listeners(self, msg: dict) -> None:
        for conn in self.listeners:
            conn.send(msg)

    def to_admins(self, msg: dict) -> None:
        for conn in self.admins:
            conn.send(msg)

    def partial(self, source_lang: str, text: str) -> None:
        """Live (unfinished) source text: shown as-is to source listeners, as 'typing' to others."""
        self.to_admins({"type": "partial", "text": text})
        now = time.monotonic()
        send_typing = bool(text) and now - self._last_typing > 1.0
        if send_typing:
            self._last_typing = now
        for conn in self.listeners:
            if conn.lang == source_lang:
                conn.send({"type": "partial", "text": text})
            elif send_typing:
                conn.send({"type": "typing"})
