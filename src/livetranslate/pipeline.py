"""Glue: audio → speech recognition (thread) → translation → phones and TTS (asyncio)."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audio.capture import AudioSource, FileSource, create_source
from .config import SettingsStore
from .server.hub import Hub
from .stt.transcriber import StreamingTranscriber, WhisperEngine
from .translate.libretranslate import LibreTranslate
from .tts.piper import SpeechService

log = logging.getLogger(__name__)


@dataclass
class Segment:
    id: int
    source: str
    created: float
    translations: dict[str, str] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        store: SettingsStore,
        translator: LibreTranslate,
        speech: SpeechService,
        hub: Hub,
        data_dir: Path,
    ):
        self.store = store
        self.translator = translator
        self.speech = speech
        self.hub = hub
        self.models_dir = data_dir / "models" / "whisper"
        self.state = "stopped"  # stopped|loading|running
        self.paused = False
        self.error = ""
        self.history: deque[Segment] = deque(maxlen=60)
        self.stats = {"asr_pass_s": 0.0, "asr_buffer_s": 0.0, "translate_ms": 0.0, "device": ""}
        self._engine: WhisperEngine | None = None
        self._engine_key: tuple | None = None
        self._source: AudioSource | None = None
        self._audio: queue.Queue[np.ndarray] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._next_id = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sentences: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._lifecycle = asyncio.Lock()

    @property
    def settings(self):
        return self.store.settings

    @property
    def source_language(self) -> str:
        return self.settings.source_language

    @property
    def level_db(self) -> float:
        return self._source.level_db if self._source else -120.0

    def status(self) -> dict:
        return {"state": self.state, "paused": self.paused, "error": self.error}

    # -- lifecycle -------------------------------------------------------------

    def ensure_worker(self) -> None:
        """Start the translation worker on the running event loop (idempotent)."""
        self._loop = asyncio.get_running_loop()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._sentence_worker())

    async def start(self) -> None:
        async with self._lifecycle:
            if self.state != "stopped":
                return
            self.ensure_worker()
            self.error = ""
            self.state = "loading"
            self._broadcast_status()
            try:
                await asyncio.to_thread(self._load_engine)
                self._source = create_source(self.settings.audio)
                self._audio = queue.Queue()
                self._source.start(self._on_audio)
            except Exception as exc:
                log.exception("Could not start")
                self.error = str(exc)
                self.state = "stopped"
                self._source = None
                self._broadcast_status()
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._asr_loop, name="asr", daemon=True)
            self._thread.start()
            self.state = "running"
            self._broadcast_status()

    async def stop(self) -> None:
        async with self._lifecycle:
            if self.state == "stopped":
                return
            self._stop.set()
            if self._source is not None:
                await asyncio.to_thread(self._source.stop)
            if self._thread is not None:
                await asyncio.to_thread(self._thread.join, 10)
            self._source = None
            self._thread = None
            self.state = "stopped"
            self.hub.partial(self.source_language, "")
            self._broadcast_status()

    async def restart(self) -> None:
        if self.state == "stopped":
            return
        await self.stop()
        await self.start()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._broadcast_status()

    def _broadcast_status(self) -> None:
        msg = {"type": "status", "running": self.state == "running", "paused": self.paused}
        self.hub.to_listeners(msg)
        self.hub.to_admins({"type": "status", **self.status()})

    def _load_engine(self) -> None:
        asr = self.settings.asr
        key = (asr.model, asr.device, asr.beam_size, self.source_language)
        if self._engine is not None and self._engine_key == key:
            return
        self._engine = None  # free GPU memory before loading another model
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._engine = WhisperEngine(asr, self.source_language, str(self.models_dir))
        self._engine_key = key
        self.stats["device"] = self._engine.device

    # -- recognition thread ----------------------------------------------------

    def _on_audio(self, chunk: np.ndarray) -> None:
        if not self.paused:
            self._audio.put(chunk)

    def _asr_loop(self) -> None:
        assert self._engine is not None and self._loop is not None
        loop = self._loop
        source_lang = self.source_language

        def on_partial(text: str) -> None:
            loop.call_soon_threadsafe(self.hub.partial, source_lang, text)

        def on_sentence(text: str) -> None:
            loop.call_soon_threadsafe(self._sentences.put_nowait, (text, time.time()))

        transcriber = StreamingTranscriber(
            self._engine.transcribe, self._engine.has_speech, on_partial, on_sentence, self.settings.asr
        )
        was_paused = False
        while not self._stop.is_set():
            try:
                transcriber.add_audio(self._audio.get(timeout=0.2))
                while True:  # drain everything that arrived while we were busy
                    transcriber.add_audio(self._audio.get_nowait())
            except queue.Empty:
                pass
            if self.paused != was_paused:
                was_paused = self.paused
                if self.paused:
                    transcriber.reset()
                continue
            source = self._source
            file_done = isinstance(source, FileSource) and source.finished.is_set() and self._audio.empty()
            if transcriber.ready() or (file_done and transcriber.buffer_seconds > 0.3):
                try:
                    transcriber.process()
                except Exception as exc:
                    log.exception("Recognition failed")
                    self.error = str(exc)
                self.stats["asr_pass_s"] = round(transcriber.last_pass_s, 2)
                self.stats["asr_buffer_s"] = round(transcriber.buffer_seconds, 1)
            if file_done and transcriber.buffer_seconds <= 0.6:
                log.info("Audio file finished")
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.stop()))
                return

    # -- translation -----------------------------------------------------------

    def wanted_languages(self) -> set[str]:
        return (self.hub.active_languages() | self.speech.active_languages()) - {self.source_language}

    async def _translate(self, seg: Segment, lang: str) -> str | None:
        if lang == self.source_language:
            return seg.source
        if lang in seg.translations:
            return seg.translations[lang]
        try:
            text = await self.translator.translate(seg.source, self.source_language, lang)
        except Exception as exc:
            log.warning("Translation to %s failed: %s", lang, exc)
            return None
        seg.translations[lang] = text
        return text

    async def _sentence_worker(self) -> None:
        while True:
            text, created = await self._sentences.get()
            seg = Segment(self._next_id, text, created)
            self._next_id += 1
            self.history.append(seg)
            src = self.source_language
            self.hub.to_admins({"type": "segment", "id": seg.id, "text": text})
            self.hub.to_language(src, {"type": "segment", "id": seg.id, "text": text, "lang": src})
            self.speech.say(src, text)

            started = time.perf_counter()
            langs = sorted(self.wanted_languages())
            results = await asyncio.gather(*(self._translate(seg, lang) for lang in langs))
            if langs:
                self.stats["translate_ms"] = round((time.perf_counter() - started) * 1000)
            for lang, translated in zip(langs, results):
                if translated:
                    self.hub.to_language(lang, {"type": "segment", "id": seg.id, "text": translated, "lang": lang})
                    self.speech.say(lang, translated)
            self.stats["latency_s"] = round(time.time() - created, 2)

    async def history_for(self, lang: str, limit: int = 15) -> list[dict]:
        segments = list(self.history)[-limit:]
        texts = await asyncio.gather(*(self._translate(seg, lang) for seg in segments))
        return [{"id": seg.id, "text": t} for seg, t in zip(segments, texts) if t]
