"""Tells songs from speech, so songs are skipped but a prayer over the piano is not.

During songs Whisper invents text and gets stuck repeating it, so it takes a classifier:
YAMNet (Google, Apache 2.0, in yamnet.onnx next to this file). It scores the sound for all
521 AudioSet classes at once. Worship music with a congregation mostly scores as "Music",
hardly as "Singing", so music without speech counts as a song too. Speech under loud music
can score low on "Speech", which is what the voice detector is for: a steady voice keeps the
text coming. When in doubt the text goes through: a stray lyric on a phone is better than a
missed prayer.

The limits come from a real service (114 min, 40 min of it music; 2026-11-01): they remove
nearly all invented text, and with real speech mixed over real music at -12 dB hold back
3-16% of the time.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..native import onnx_session

log = logging.getLogger(__name__)

MODEL = Path(__file__).with_name("yamnet.onnx")
WINDOW_S = 2.0  # how much recent audio each decision looks at
MIN_SAMPLES = 16000  # one YAMNet frame needs about a second

# Indexes in YAMNet's class list. No genres like "Christian music": those also score high
# on a band or piano without any voice, and speech over that has to get through.
SPEECH = [0, 1, 2, 3]  # speech, child speech, conversation, narration
SINGING = [24, 25, 27, 29, 30, 249, 250]  # singing, choir, chant, child and synthetic singing, vocal music, a capella
MUSIC = [132, 133]  # music, musical instrument

MIN_SINGING = 0.25
VOICED_PASSES = 3
ON_AFTER = 2  # passes in a row (about a second each) before text is held back
OFF_AFTER = 2


@dataclass
class SoundScores:
    speech: float
    singing: float
    music: float


@dataclass(frozen=True)
class SongLimits:
    min_music: float
    max_speech: float
    max_voiced: float  # share of the recent audio the voice detector calls speech


# The slider in Settings → Church, from "translate more" to "skip more". 0 is the tuned default.
SONG_LEVELS = {
    -3: SongLimits(0.7, 0.1, 0.2),
    -2: SongLimits(0.6, 0.15, 0.25),
    -1: SongLimits(0.5, 0.2, 0.3),
    0: SongLimits(0.4, 0.3, 0.4),
    1: SongLimits(0.35, 0.4, 0.5),
    2: SongLimits(0.3, 0.5, 0.6),
    3: SongLimits(0.25, 0.6, 0.7),
}


HOTWORD_MAX_MUSIC = 0.5  # from this music score on, without more speech, Whisper gets no glossary words


def is_music(scores: SoundScores) -> bool:
    """Mostly music and no talking: Whisper would write down its glossary words instead.

    In the service of 8 Nov 2026 every made-up "De Kandelaar, de Kandelaar" came at music 0.5–0.98
    with speech near 0; real mentions of the name were said with speech above music.
    """
    return scores.music >= HOTWORD_MAX_MUSIC and scores.music > scores.speech


# A trained model says "song" from this chance on. The church's own training replaces the limits above.
TRAINED_LIMIT = 0.5


def song_chance(weights: np.ndarray, bias: float, description: np.ndarray) -> float:
    """How sure a trained model is that this is a song, from YAMNet's description of the sound."""
    return float(1 / (1 + np.exp(-np.clip(np.dot(description, weights) + bias, -30, 30))))


class SoundClassifier:
    def __init__(self, gpu: bool = True):
        self.session = onnx_session(MODEL, gpu)

    def analyse(self, audio: np.ndarray) -> tuple[SoundScores, np.ndarray]:
        """The scores, and the 1024 numbers YAMNet describes the sound with, which a trained model reads."""
        if len(audio) < MIN_SAMPLES:
            audio = np.pad(audio, (0, MIN_SAMPLES - len(audio)))
        frames, description = self.session.run(["output_0", "output_1"], {"waveform": audio.astype(np.float32)})
        mean = frames.mean(axis=0)
        scores = SoundScores(float(mean[SPEECH].max()), float(mean[SINGING].max()), float(mean[MUSIC].max()))
        return scores, description.mean(axis=0)

    def scores(self, audio: np.ndarray) -> SoundScores:
        return self.analyse(audio)[0]


class SingingGate:
    """Smooths the per-pass scores into one yes/no: is this a song right now?"""

    def __init__(self, limits: SongLimits = SONG_LEVELS[0]) -> None:
        self.limits = limits
        self.singing = False
        self._streak = 0
        self._voiced: deque[float] = deque(maxlen=VOICED_PASSES)

    def update(self, scores: SoundScores, voiced: float = 0.0) -> bool:
        self._voiced.append(voiced)
        music = scores.music >= self.limits.min_music and scores.speech < self.limits.max_speech
        sung = scores.singing >= MIN_SINGING and scores.singing > scores.speech
        heard = (music or sung) and sum(self._voiced) / len(self._voiced) < self.limits.max_voiced
        return self.decide(heard)

    def decide(self, heard: bool) -> bool:
        """One pass's verdict, from these rules or from a trained model; a single one doesn't change anything."""
        self._streak = self._streak + 1 if heard != self.singing else 0
        if self._streak >= (OFF_AFTER if self.singing else ON_AFTER):
            self.singing = heard
            self._streak = 0
        return self.singing
