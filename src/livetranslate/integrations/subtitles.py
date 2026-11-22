"""What sending subtitles to streaming software (vMix, OBS Studio) has in common."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def wrap(text: str, width: int) -> list[str]:
    """Lines of at most width characters, broken between words (a word longer than that is cut)."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if not word:
            continue
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def tail(text: str, width: int, max_lines: int = 2) -> list[str]:
    """The end of a sentence that is too long, after an ellipsis, in at most max_lines lines."""
    words = text.split()
    for start in range(1, len(words)):
        lines = wrap("…" + " ".join(words[start:]), width)
        if len(lines) <= max_lines:
            return lines
    return wrap("…" + text.strip()[-(width * max_lines - 1) :], width)[-max_lines:]


def compose(previous: str | None, current: str, line_chars: int) -> str:
    """Rolling subtitle of at most two lines of line_chars each, broken here so the title never
    wraps them into a third: a one-line previous sentence above a one-line current one, otherwise
    the current sentence alone (its end, when it doesn't fit in two lines)."""
    lines = wrap(current, line_chars)
    if len(lines) > 2:
        lines = tail(current, line_chars)
    if len(lines) == 1 and previous and len(previous) <= line_chars:
        lines = [previous, *lines]
    return "\n".join(lines)


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
        width = self.settings.line_chars
        text = compose(self.previous, sentence, width)
        current = wrap(sentence, width)
        self.previous = current[0] if len(current) == 1 else None  # may stay above the next one
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
