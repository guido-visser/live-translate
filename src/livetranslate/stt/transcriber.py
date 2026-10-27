"""Streaming speech recognition on top of faster-whisper.

Whisper is not a streaming model, so we use the LocalAgreement policy (as in
whisper_streaming): the growing audio buffer is re-transcribed every second and
words that two consecutive passes agree on are committed. Committed words are
grouped into sentences, which are what gets translated. A pause in speech
finalizes whatever is still open, which keeps the delay short.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ..config import AsrSettings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Phrases Whisper tends to invent on silence or music (it was trained on subtitles).
HALLUCINATIONS = [
    "ondertiteling",
    "ondertiteld door",
    "amara.org",
    "bedankt voor het kijken",
    "tv gelderland",
    "subtitles by",
    "thanks for watching",
    "продолжение следует",
    "субтитры",
]

ABBREVIATIONS = {"bijv.", "o.a.", "d.w.z.", "enz.", "joh.", "mr.", "dr.", "ds.", "st.", "vs.", "ca.", "nl."}
SENTENCE_END = re.compile(r"[.!?…][\"'”’»)]*$")
CLAUSE_END = re.compile(r"[,;:][\"'”’»)]*$")


@dataclass
class Word:
    start: float
    end: float
    text: str


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def join_words(words: list[Word]) -> str:
    return re.sub(r"\s+", " ", "".join(w.text if w.text.startswith(" ") else " " + w.text for w in words)).strip()


class HypothesisBuffer:
    """LocalAgreement-2: commit the longest common prefix of two consecutive hypotheses."""

    def __init__(self) -> None:
        self.committed: list[Word] = []  # committed words still inside the audio buffer
        self.buffer: list[Word] = []  # previous hypothesis, not yet committed
        self.new: list[Word] = []
        self.last_committed_time = 0.0

    def insert(self, words: list[Word], offset: float) -> None:
        shifted = [Word(w.start + offset, w.end + offset, w.text) for w in words]
        self.new = [w for w in shifted if w.start > self.last_committed_time - 0.1]
        if not self.new or not self.committed:
            return
        if abs(self.new[0].start - self.last_committed_time) < 1.0:
            # Drop words at the start that repeat the tail of what is already committed.
            for n in range(min(len(self.committed), len(self.new), 5), 0, -1):
                tail = [_norm(w.text) for w in self.committed[-n:]]
                head = [_norm(w.text) for w in self.new[:n]]
                if tail == head:
                    self.new = self.new[n:]
                    break

    def flush(self) -> list[Word]:
        commit: list[Word] = []
        while self.new and self.buffer and _norm(self.new[0].text) == _norm(self.buffer[0].text):
            word = self.new.pop(0)
            self.buffer.pop(0)
            commit.append(word)
            self.last_committed_time = word.end
        self.buffer = self.new
        self.new = []
        self.committed.extend(commit)
        return commit

    def commit_all(self) -> list[Word]:
        words, self.buffer = self.buffer, []
        if words:
            self.last_committed_time = words[-1].end
        self.committed.extend(words)
        return words

    def pop_committed(self, time_s: float) -> None:
        while self.committed and self.committed[0].end <= time_s:
            self.committed.pop(0)


TranscribeFn = Callable[[np.ndarray, str], list[Word]]
SpeechFn = Callable[[np.ndarray], bool]


class StreamingTranscriber:
    """Feed it 16 kHz audio; it calls on_partial with the open text and on_sentence per sentence."""

    def __init__(
        self,
        transcribe: TranscribeFn,
        has_speech: SpeechFn,
        on_partial: Callable[[str], None],
        on_sentence: Callable[[str], None],
        settings: AsrSettings,
    ) -> None:
        self.transcribe = transcribe
        self.has_speech = has_speech
        self.on_partial = on_partial
        self.on_sentence = on_sentence
        self.settings = settings
        self.audio = np.zeros(0, dtype=np.float32)
        self.offset = 0.0
        self.hyp = HypothesisBuffer()
        self.pending: list[Word] = []  # committed, not yet emitted as a sentence
        self.context = ""
        self.last_sentence_end = 0.0
        self.last_pass_s = 0.0
        self._new_samples = 0
        self._last_partial = ""

    @property
    def end_time(self) -> float:
        return self.offset + len(self.audio) / SAMPLE_RATE

    @property
    def buffer_seconds(self) -> float:
        return len(self.audio) / SAMPLE_RATE

    def add_audio(self, chunk: np.ndarray) -> None:
        self.audio = np.concatenate([self.audio, chunk])
        self._new_samples += len(chunk)

    def ready(self) -> bool:
        return self._new_samples >= self.settings.min_chunk_s * SAMPLE_RATE

    def reset(self) -> None:
        """Discard everything (used when pausing)."""
        self._trim_to(self.end_time)
        self.hyp = HypothesisBuffer()
        self.pending = []
        self._new_samples = 0
        self._partial("")

    def process(self) -> None:
        self._new_samples = 0
        if not len(self.audio):
            return
        open_text = bool(self.pending or self.hyp.buffer)
        if not open_text and not self.has_speech(self.audio):
            self._trim_to(self.end_time - 0.5)
            return

        started = time.perf_counter()
        words = self.transcribe(self.audio, self.context)
        self.last_pass_s = time.perf_counter() - started

        self.hyp.insert(words, self.offset)
        self.pending += self.hyp.flush()

        tail = self.audio[-int(self.settings.silence_commit_s * SAMPLE_RATE) :]
        if self.buffer_seconds > self.settings.silence_commit_s + 0.2 and not self.has_speech(tail):
            # The speaker paused: everything heard so far is final.
            self.pending += self.hyp.commit_all()
            self._emit_sentences()
            if self.pending:
                self._emit(len(self.pending))
            self._trim_to(self.end_time)
            self.hyp = HypothesisBuffer()
            self.hyp.last_committed_time = self.offset
        else:
            self._emit_sentences()
            self._force_long_sentence()
            self._trim_buffer()
        self._partial(join_words(self.pending + self.hyp.buffer))

    def _partial(self, text: str) -> None:
        if text != self._last_partial:
            self._last_partial = text
            self.on_partial(text)

    def _emit_sentences(self) -> None:
        while True:
            for i, word in enumerate(self.pending):
                text = word.text.strip()
                if SENTENCE_END.search(text) and text.lower() not in ABBREVIATIONS:
                    self._emit(i + 1)
                    break
            else:
                return

    def _force_long_sentence(self) -> None:
        if not self.pending:
            return
        if self.end_time - self.pending[0].start < self.settings.max_sentence_s:
            return
        cut = len(self.pending)
        for i in range(len(self.pending) - 1, 2, -1):
            if CLAUSE_END.search(self.pending[i].text.strip()):
                cut = i + 1
                break
        self._emit(cut)

    def _emit(self, n: int) -> None:
        words, self.pending = self.pending[:n], self.pending[n:]
        text = join_words(words)
        self.last_sentence_end = words[-1].end
        if not text or not _norm(text):
            return
        self.context = (self.context + " " + text)[-200:]
        self.on_sentence(text)

    def _trim_buffer(self) -> None:
        if self.buffer_seconds > 10 and self.last_sentence_end > self.offset + 1:
            self._trim_to(self.last_sentence_end)
        elif self.buffer_seconds > 20:
            self._trim_to(max(self.hyp.last_committed_time, self.end_time - 10))

    def _trim_to(self, time_s: float) -> None:
        cut = int((time_s - self.offset) * SAMPLE_RATE)
        if cut <= 0:
            return
        self.audio = self.audio[cut:]
        self.offset = time_s
        self.hyp.pop_committed(time_s)


class WhisperEngine:
    """Loads a faster-whisper model and exposes the two functions the transcriber needs."""

    def __init__(self, settings: AsrSettings, language: str, download_root: str):
        from faster_whisper import WhisperModel

        from .cuda import cuda_available, setup_cuda_dll_paths

        setup_cuda_dll_paths()
        device = settings.device
        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        compute_type = "int8_float16" if device == "cuda" else "int8"
        log.info("Loading Whisper %s on %s (%s)", settings.model, device, compute_type)
        try:
            self.model = WhisperModel(
                settings.model, device=device, compute_type=compute_type, download_root=download_root, cpu_threads=4
            )
        except Exception:
            if device != "cuda":
                raise
            log.exception("Loading on the GPU failed, falling back to the CPU")
            device, compute_type = "cpu", "int8"
            self.model = WhisperModel(
                settings.model, device=device, compute_type=compute_type, download_root=download_root, cpu_threads=4
            )
        self.device = device
        self.language = language
        self.beam_size = settings.beam_size
        self._vad_options = None

    def transcribe(self, audio: np.ndarray, prompt: str) -> list[Word]:
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            initial_prompt=prompt or None,
            beam_size=self.beam_size,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        words: list[Word] = []
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            lowered = seg.text.lower()
            if any(h in lowered for h in HALLUCINATIONS):
                continue
            words += [Word(w.start, w.end, w.word) for w in seg.words or []]
        return words

    def has_speech(self, audio: np.ndarray) -> bool:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        if self._vad_options is None:
            self._vad_options = VadOptions(min_speech_duration_ms=150, min_silence_duration_ms=100, speech_pad_ms=0)
        return bool(get_speech_timestamps(audio, self._vad_options))
