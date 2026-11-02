"""What sending subtitles to streaming software (vMix, OBS Studio) has in common."""

from __future__ import annotations

import logging

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


class SubtitleOutput:
    """Rolling subtitles + overlay control. Failures are reported, never raised."""

    name = ""
    errors: tuple[type[Exception], ...] = ()

    def __init__(self) -> None:
        self.previous: str | None = None
        self.status = {"ok": None, "error": "", "last_text": "", "overlay": None}

    @property
    def settings(self):
        raise NotImplementedError

    @property
    def ready(self) -> bool:
        """It knows where the text goes."""
        raise NotImplementedError

    @property
    def active(self) -> bool:
        return self.settings.enabled and self.ready

    async def _set_text(self, text: str) -> None:
        raise NotImplementedError

    async def _set_overlay(self, on: bool) -> None:
        raise NotImplementedError

    async def _guard(self, action) -> bool:
        try:
            await action
        except self.errors as exc:
            message = str(exc) or exc.__class__.__name__
            if self.status["error"] != message:
                log.warning("%s: %s", self.name, message)
            self.status.update(ok=False, error=message)
            return False
        self.status.update(ok=True, error="")
        return True

    async def show(self, text: str) -> bool:
        if not self.ready:
            return False
        ok = await self._guard(self._set_text(text))
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
        if not self.ready:
            return False
        ok = await self._guard(self._set_overlay(on))
        if ok:
            self.status["overlay"] = on
        return ok
