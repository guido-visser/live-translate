"""Fits the song detection to a church's sound, using a logged service.

The log holds what the sound classifier scored at every pass, so the decisions can be replayed
for every level of the slider in a moment, without running a model. Nothing here is retrained.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import soundfile as sf

from .stt.sound import SONG_LEVELS, SingingGate, SoundScores

MAX_GAP_S = 10.0  # no passes for this long: paused or stopped, so the song isn't assumed to go on
LIKELY_SPEECH = 0.5  # a skipped moment with this "Speech" score was probably someone talking
LIKELY_VOICED = 0.8  # or with this much of it called speech by the voice detector
SPEECH_WEIGHT = 3  # a skipped prayer is worse than a stray lyric
CLIP_S = 20.0
CLIP_LEAD_S = 2.0  # a decision is about the audio just before it


def read_events(folder: Path) -> list[dict]:
    """The log's events; the last line can be cut off if the app didn't stop normally."""
    events = []
    for line in (folder / "events.jsonl").read_text("utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def list_services(root: Path, running: Path | None = None) -> list[dict]:
    """Logged services the sound classifier was on for, newest first."""
    found = []
    for folder in sorted(root.glob("*"), reverse=True):
        if folder == running or not (folder / "events.jsonl").is_file():
            continue
        events = read_events(folder)
        if any(e.get("kind") == "sound" for e in events):
            found.append({"name": folder.name, "duration": events[-1]["t"], "audio": (folder / "audio.flac").is_file()})
    return found


def _replay(sounds: list[dict], level: int) -> dict:
    """What this level would have skipped: stretches of [start, end] seconds and the totals."""
    gate = SingingGate(SONG_LEVELS[level])
    skipped: list[list[float]] = []
    start = None
    skipped_s = speech_s = 0.0
    last = sounds[0]["t"] if sounds else 0.0
    for e in sounds:
        gap = e["t"] - last
        if gap > MAX_GAP_S:
            if start is not None:
                skipped.append([start, last])
                start = None
            gate = SingingGate(SONG_LEVELS[level])
            gap = 0.0
        on = gate.update(SoundScores(e["speech"], e["singing"], e["music"]), e["voiced"])
        if on:
            skipped_s += gap
            if e["speech"] >= LIKELY_SPEECH or e["voiced"] >= LIKELY_VOICED:
                speech_s += gap
        if on and start is None:
            start = e["t"]
        elif not on and start is not None:
            skipped.append([start, e["t"]])
            start = None
        last = e["t"]
    if start is not None:
        skipped.append([start, last])
    return {"skipped": skipped, "skipped_s": round(skipped_s), "speech_s": round(speech_s)}


def analyse(folder: Path) -> dict:
    events = read_events(folder)
    sounds = [e for e in events if e.get("kind") == "sound"]
    return {
        "name": folder.name,
        "duration": events[-1]["t"],
        "audio": (folder / "audio.flac").is_file(),
        "marks": [e["t"] for e in events if e.get("kind") == "mark"],
        "sentences": [{"t": e["t"], "text": e["text"]} for e in events if e.get("kind") == "sentence"],
        "levels": {level: _replay(sounds, level) for level in SONG_LEVELS},
    }


def _overlap(stretches: list[list[float]], start: float, end: float) -> float:
    return sum(max(0.0, min(b, end) - max(a, start)) for a, b in stretches)


def best_level(analysis: dict, labels: list[dict]) -> int:
    """The level that gets the stretches the operator labelled most right; the default when it's a tie."""

    def wrong(level: int) -> float:
        skipped = analysis["levels"][level]["skipped"]
        total = 0.0
        for label in labels:
            overlap = _overlap(skipped, label["start"], label["end"])
            if label["kind"] == "speech":
                total += overlap * SPEECH_WEIGHT
            else:
                total += label["end"] - label["start"] - overlap
        return total

    return min(SONG_LEVELS, key=lambda level: (wrong(level), abs(level), level))


def audio_clip(folder: Path, t: float) -> bytes:
    """A short WAV from this moment. The recording stands still during a pause and the log's clock doesn't."""
    position = t - CLIP_LEAD_S
    paused_at = None
    for e in read_events(folder):
        if e.get("kind") == "paused":
            paused_at = e["t"]
        elif e.get("kind") == "resumed" and paused_at is not None:
            if paused_at < t:
                position -= min(e["t"], t) - paused_at
            paused_at = None
    out = io.BytesIO()
    with sf.SoundFile(folder / "audio.flac") as audio:
        audio.seek(min(max(0, int(position * audio.samplerate)), audio.frames))
        sf.write(out, audio.read(int(CLIP_S * audio.samplerate), dtype="int16"), audio.samplerate, format="WAV")
    return out.getvalue()
