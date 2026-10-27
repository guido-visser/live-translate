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
from .integrations.vmix import VmixOutput
from .server.hub import Hub
from .stt.transcriber import StreamingTranscriber, WhisperEngine
from .translate.service import TranslationService
from .tts.piper import SpeechService

log = logging.getLogger(__name__)


try:
    from faster_whisper.tokenizer import _LANGUAGE_CODES as WHISPER_LANGUAGES
except ImportError:  # pragma: no cover
    WHISPER_LANGUAGES = ()


@dataclass
class Segment:
    id: int
    source: str
    created: float
    lang: str  # language the speaker used for this sentence
    translations: dict[str, str] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        store: SettingsStore,
        translator: TranslationService,
        speech: SpeechService,
        hub: Hub,
        data_dir: Path,
        vmix: VmixOutput | None = None,
    ):
        self.store = store
        self.translator = translator
        self.speech = speech
        self.hub = hub
        self.vmix = vmix
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
        # What the person at the microphone speaks right now; the operator switches it
        # (e.g. to Russian for a testimony). Read by the recognition thread.
        self.speaker_language = self.settings.source_language
        self._sentences: asyncio.Queue[tuple[str, float, str]] = asyncio.Queue()
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
        return {
            "state": self.state,
            "paused": self.paused,
            "error": self.error,
            "speaker_language": self.speaker_language,
        }

    def speaker_languages(self) -> list[str]:
        """The main language plus the listener languages Whisper can recognize."""
        main = self.source_language
        others = [l for l in self.settings.target_languages if l != main and l in WHISPER_LANGUAGES]
        return [main, *others]

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
            self.hub.partial(self.speaker_language, "")
            self._broadcast_status()

    async def restart(self) -> None:
        if self.state == "stopped":
            return
        await self.stop()
        await self.start()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._broadcast_status()

    async def set_speaker_language(self, lang: str) -> None:
        """Switch recognition to another language without reloading the model."""
        if lang not in self.speaker_languages():
            raise ValueError(f"Unsupported speaker language: {lang}")
        if lang == self.speaker_language:
            return
        self.speaker_language = lang  # the recognition thread picks this up
        self._broadcast_status()
        vmix = self.vmix
        if vmix is None or not vmix.active:
            return
        if lang != self.source_language:
            await vmix.clear()  # don't show the end of an earlier testimony
            if vmix.settings.auto_overlay:
                await vmix.overlay(True)
        elif vmix.settings.auto_overlay:
            await vmix.overlay(False)

    def _broadcast_status(self) -> None:
        msg = {"type": "status", "running": self.state == "running", "paused": self.paused}
        self.hub.to_listeners(msg)
        self.hub.to_admins({"type": "status", **self.status()})

    def _load_engine(self) -> None:
        asr = self.settings.asr
        key = (asr.model, asr.device, asr.beam_size)  # language is switched per pass, no reload
        if self._engine is not None and self._engine_key == key:
            return
        self._engine = None  # free GPU memory before loading another model
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._engine = WhisperEngine(asr, self.speaker_language, str(self.models_dir))
        self._engine_key = key
        self.stats["device"] = self._engine.device

    # -- recognition thread ----------------------------------------------------

    def _on_audio(self, chunk: np.ndarray) -> None:
        if not self.paused:
            self._audio.put(chunk)

    def _asr_loop(self) -> None:
        assert self._engine is not None and self._loop is not None
        loop = self._loop
        engine = self._engine
        engine.language = self.speaker_language

        def on_partial(text: str) -> None:
            loop.call_soon_threadsafe(self.hub.partial, engine.language, text)

        def on_sentence(text: str) -> None:
            loop.call_soon_threadsafe(self._sentences.put_nowait, (text, time.time(), engine.language))

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
            if self.speaker_language != engine.language:
                # Finish what the previous speaker said, then listen in the new language.
                transcriber.finalize()
                on_partial("")
                engine.language = self.speaker_language
                transcriber.reset()
                # The prompt holds earlier text; in the old language it drags Whisper back into it.
                transcriber.context = ""
                log.info("Speaker language is now %s", engine.language)
                continue
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

    def wanted_languages(self, seg: Segment) -> set[str]:
        wanted = self.hub.active_languages() | self.speech.active_languages()
        if self._vmix_wants(seg):
            wanted.add(self.source_language)
        return wanted - {seg.lang}

    def _vmix_wants(self, seg: Segment) -> bool:
        """vMix shows the main-language translation while someone speaks another language."""
        return self.vmix is not None and self.vmix.active and seg.lang != self.source_language

    async def _translate(self, seg: Segment, lang: str) -> str | None:
        if lang == seg.lang:
            return seg.source
        if lang in seg.translations:
            return seg.translations[lang]
        try:
            text = await self.translator.translate(seg.source, seg.lang, lang)
        except Exception as exc:
            log.warning("Translation to %s failed: %s", lang, exc)
            return None
        seg.translations[lang] = text
        return text

    async def _sentence_worker(self) -> None:
        while True:
            item = await self._sentences.get()
            try:
                await self._handle_sentence(*item)
            except Exception:
                log.exception("Handling a sentence failed")

    async def _handle_sentence(self, text: str, created: float, src: str) -> None:
        seg = Segment(self._next_id, text, created, src)
        self._next_id += 1
        self.history.append(seg)
        self.hub.to_admins({"type": "segment", "id": seg.id, "text": text, "lang": src})
        self.hub.to_language(src, {"type": "segment", "id": seg.id, "text": text, "lang": src})
        self.speech.say(src, text)

        started = time.perf_counter()
        langs = sorted(self.wanted_languages(seg))
        results = await asyncio.gather(*(self._translate(seg, lang) for lang in langs))
        if langs:
            self.stats["translate_ms"] = round((time.perf_counter() - started) * 1000)
        for lang, translated in zip(langs, results):
            if translated:
                self.hub.to_language(lang, {"type": "segment", "id": seg.id, "text": translated, "lang": lang})
                self.speech.say(lang, translated)
        if self._vmix_wants(seg):
            main = dict(zip(langs, results)).get(self.source_language)
            if main:
                await self.vmix.push(main)
        self.stats["latency_s"] = round(time.time() - created, 2)

    def clear_translations(self) -> None:
        for seg in self.history:
            seg.translations.clear()

    async def history_for(self, lang: str, limit: int = 15) -> list[dict]:
        segments = list(self.history)[-limit:]
        texts = await asyncio.gather(*(self._translate(seg, lang) for seg in segments))
        return [{"id": seg.id, "text": t} for seg, t in zip(segments, texts) if t]
