"""Spoken translations: Piper TTS, streamed per language as a continuous MP3 "radio".

A plain <audio> element playing an endless stream keeps playing when the phone
screen locks (unlike Web Audio driven by JavaScript), so listeners can pocket
their phone and just listen with earphones.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from pathlib import Path

import httpx
import numpy as np
import soxr

log = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# One good default voice per language (path inside the piper-voices repository).
VOICES = {
    "ar": "ar/ar_JO/kareem/medium/ar_JO-kareem-medium",
    "cs": "cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium",
    "da": "da/da_DK/talesyntese/medium/da_DK-talesyntese-medium",
    "de": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "el": "el/el_GR/rapunzelina/low/el_GR-rapunzelina-low",
    "en": "en/en_US/lessac/medium/en_US-lessac-medium",
    "es": "es/es_ES/davefx/medium/es_ES-davefx-medium",
    "fa": "fa/fa_IR/amir/medium/fa_IR-amir-medium",
    "fi": "fi/fi_FI/harri/medium/fi_FI-harri-medium",
    "fr": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    "hu": "hu/hu_HU/anna/medium/hu_HU-anna-medium",
    "it": "it/it_IT/paola/medium/it_IT-paola-medium",
    "nl": "nl/nl_BE/nathalie/medium/nl_BE-nathalie-medium",
    "pl": "pl/pl_PL/gosia/medium/pl_PL-gosia-medium",
    "pt": "pt/pt_BR/faber/medium/pt_BR-faber-medium",
    "ro": "ro/ro_RO/mihai/medium/ro_RO-mihai-medium",
    "ru": "ru/ru_RU/irina/medium/ru_RU-irina-medium",
    "sk": "sk/sk_SK/lili/medium/sk_SK-lili-medium",
    "sv": "sv/sv_SE/nst/medium/sv_SE-nst-medium",
    "tr": "tr/tr_TR/dfki/medium/tr_TR-dfki-medium",
    "uk": "uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium",
    "vi": "vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium",
    "zh": "zh/zh_CN/huayan/medium/zh_CN-huayan-medium",
}

STREAM_RATE = 22050
FRAME_SECONDS = 0.1
FRAME_SAMPLES = int(STREAM_RATE * FRAME_SECONDS)
BITRATE_KBPS = 48


def _new_encoder():
    import lameenc

    enc = lameenc.Encoder()
    enc.set_bit_rate(BITRATE_KBPS)
    enc.set_in_sample_rate(STREAM_RATE)
    enc.set_channels(1)
    enc.set_quality(5)
    return enc


def _silence_preroll(seconds: float) -> bytes:
    enc = _new_encoder()
    data = enc.encode(np.zeros(int(STREAM_RATE * seconds), dtype=np.int16).tobytes())
    return bytes(data) + bytes(enc.flush())


class VoiceBank:
    """Downloads Piper voices on demand and synthesizes speech (blocking; run in a thread)."""

    def __init__(self, voices_dir: Path):
        self.dir = voices_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._voices: dict = {}
        self._failed: set[str] = set()
        self._lock = threading.Lock()

    def has_voice(self, lang: str) -> bool:
        return lang in VOICES and lang not in self._failed

    def _voice(self, lang: str):
        with self._lock:
            if lang in self._voices:
                return self._voices[lang]
            from piper import PiperVoice

            name = VOICES[lang]
            onnx = self.dir / (name.rsplit("/", 1)[1] + ".onnx")
            meta = onnx.with_suffix(".onnx.json")
            try:
                for target, suffix in ((onnx, ".onnx"), (meta, ".onnx.json")):
                    if not target.exists():
                        log.info("Downloading Piper voice %s%s", name, suffix)
                        tmp = target.with_suffix(target.suffix + ".part")
                        with httpx.stream("GET", HF_BASE + name + suffix, follow_redirects=True, timeout=60) as r:
                            r.raise_for_status()
                            with open(tmp, "wb") as f:
                                for chunk in r.iter_bytes():
                                    f.write(chunk)
                        tmp.replace(target)
                voice = PiperVoice.load(onnx, config_path=meta)
            except Exception:
                log.exception("Could not load Piper voice for %s", lang)
                self._failed.add(lang)
                raise
            self._voices[lang] = voice
            return voice

    def synthesize(self, lang: str, text: str, length_scale: float = 1.0) -> np.ndarray:
        """Returns int16 mono audio at STREAM_RATE."""
        from piper import SynthesisConfig

        from piper.config import PhonemeType

        voice = self._voice(lang)
        if voice.config.phoneme_type == PhonemeType.TEXT:
            text = text.lower()  # character-based voices (e.g. Ukrainian) only know lowercase letters
        parts, rate = [], STREAM_RATE
        for chunk in voice.synthesize(text, syn_config=SynthesisConfig(length_scale=length_scale)):
            parts.append(chunk.audio_float_array)
            rate = chunk.sample_rate
        if not parts:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(parts).astype(np.float32)
        if rate != STREAM_RATE:
            audio = soxr.resample(audio, rate, STREAM_RATE)
        return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


class LanguageStream:
    """One endless MP3 stream per language, paced in real time, silence when idle."""

    def __init__(self, lang: str, bank: VoiceBank):
        self.lang = lang
        self.bank = bank
        self.clients: set[asyncio.Queue] = set()
        self._texts: asyncio.Queue[str] = asyncio.Queue()
        self._pcm: deque[np.ndarray] = deque()
        self._pcm_samples = 0
        self._tasks: list[asyncio.Task] = []

    @property
    def backlog_seconds(self) -> float:
        return self._pcm_samples / STREAM_RATE

    def say(self, text: str) -> None:
        while self._texts.qsize() >= 4:  # far behind: skip the oldest sentence
            self._texts.get_nowait()
        self._texts.put_nowait(text)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        queue.put_nowait(_silence_preroll(0.5))
        self.clients.add(queue)
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._pace()), asyncio.create_task(self._synth())]
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.clients.discard(queue)
        if not self.clients:
            for task in self._tasks:
                task.cancel()
            self._tasks = []
            self._pcm.clear()
            self._pcm_samples = 0
            while not self._texts.empty():
                self._texts.get_nowait()

    async def _synth(self) -> None:
        gap = np.zeros(int(STREAM_RATE * 0.3), dtype=np.int16)
        while True:
            text = await self._texts.get()
            # Speak a bit faster when we are falling behind the speaker.
            backlog = self.backlog_seconds
            length_scale = 1.0 if backlog < 3 else 0.88 if backlog < 8 else 0.78
            try:
                audio = await asyncio.to_thread(self.bank.synthesize, self.lang, text, length_scale)
            except Exception:
                log.exception("TTS failed for %s", self.lang)
                continue
            for part in (audio, gap):
                self._pcm.append(part)
                self._pcm_samples += len(part)

    def _take_frame(self) -> np.ndarray:
        out = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        filled = 0
        while filled < FRAME_SAMPLES and self._pcm:
            head = self._pcm[0]
            n = min(FRAME_SAMPLES - filled, len(head))
            out[filled : filled + n] = head[:n]
            filled += n
            if n == len(head):
                self._pcm.popleft()
            else:
                self._pcm[0] = head[n:]
            self._pcm_samples -= n
        return out

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        encoder = _new_encoder()
        started = loop.time()
        frames = 0
        while True:
            # Catch up if the event loop was busy, so the stream stays real time.
            while frames * FRAME_SECONDS <= loop.time() - started:
                data = bytes(encoder.encode(self._take_frame().tobytes()))
                frames += 1
                if data:
                    for queue in list(self.clients):
                        if queue.full():
                            continue  # slow client; it will skip a bit of audio
                        queue.put_nowait(data)
            await asyncio.sleep(max(0.0, started + frames * FRAME_SECONDS - loop.time()))


class SpeechService:
    def __init__(self, voices_dir: Path):
        self.bank = VoiceBank(voices_dir)
        self.streams: dict[str, LanguageStream] = {}
        self.enabled = True

    def has_voice(self, lang: str) -> bool:
        return self.enabled and self.bank.has_voice(lang)

    def bank_languages(self) -> list[str]:
        return [lang for lang in VOICES if self.bank.has_voice(lang)]

    def listener_counts(self) -> dict[str, int]:
        return {lang: len(s.clients) for lang, s in self.streams.items() if s.clients}

    def active_languages(self) -> set[str]:
        return {lang for lang, s in self.streams.items() if s.clients}

    def say(self, lang: str, text: str) -> None:
        stream = self.streams.get(lang)
        if self.enabled and stream is not None and stream.clients:
            stream.say(text)

    def stream(self, lang: str) -> LanguageStream:
        if lang not in self.streams:
            self.streams[lang] = LanguageStream(lang, self.bank)
        return self.streams[lang]

    async def warm_up(self, lang: str) -> None:
        """Download/load the voice ahead of time so the first sentence isn't delayed."""
        if self.has_voice(lang):
            try:
                await asyncio.to_thread(self.bank._voice, lang)
            except Exception:
                pass
