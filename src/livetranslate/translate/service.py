"""Routes translation to the engine chosen on the admin page (LibreTranslate or NLLB)."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from .glossary import Glossary
from .libretranslate import LibreTranslate
from .nllb import NllbTranslator

log = logging.getLogger(__name__)


class TranslationService:
    def __init__(self, settings: Settings, data_dir: Path):
        self.libre = LibreTranslate(settings, data_dir)
        self.nllb = NllbTranslator(settings, data_dir)
        self.engine = settings.translation.engine
        self.glossary = Glossary(settings.glossary)

    @property
    def active(self) -> LibreTranslate | NllbTranslator:
        return self.nllb if self.engine == "nllb" else self.libre

    # The admin page and listener language lists read these from the active engine.
    @property
    def status(self) -> str:
        return self.active.status

    @property
    def detail(self) -> str:
        return self.active.detail

    @property
    def languages(self) -> list[dict]:
        return self.active.languages

    @property
    def needs_restart(self) -> bool:
        return self.active.needs_restart

    def targets_for(self, source: str) -> list[str]:
        return self.active.targets_for(source)

    def name_of(self, code: str) -> str:
        return self.active.name_of(code)

    async def translate(self, text: str, source: str, target: str) -> str:
        engine = self.active
        prepared, terms = self.glossary.prepare(text, source, target, inline=engine is self.nllb)
        if prepared == text:
            return await engine.translate(text, source, target)
        result = await engine.translate(prepared, source, target)
        if not terms:
            return result
        restored = self.glossary.restore(result, terms)
        if restored is None:
            log.info("Glossary placeholder lost in %r; translating without the glossary", result)
            return await engine.translate(text, source, target)
        return restored

    async def start(self) -> None:
        await self.active.start()

    async def restart(self) -> None:
        await self.active.restart()

    async def apply(self, old: Settings, new: Settings) -> None:
        """Bring the engines in line with changed settings."""
        self.libre.configure(new)
        self.nllb.configure(new)
        if new.translation.engine != self.engine:
            previous = self.active
            self.engine = new.translation.engine
            log.info("Switching translation engine to %s", self.engine)
            await previous.stop()  # free its memory
            await self.active.start()
        elif self.active is self.libre and (
            self.libre.needs_restart or new.libretranslate_url != old.libretranslate_url
        ):
            await self.libre.restart()
        elif self.active is self.nllb and self.nllb.needs_restart:
            await self.nllb.restart()

    async def close(self) -> None:
        await self.libre.close()
        await self.nllb.close()
