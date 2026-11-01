"""An optional log of one service, to find out afterwards what went wrong and improve it.

Off unless the operator turns it on: the app otherwise stores nothing of what is said.
Everything stays on this PC in one folder per service: events.jsonl (sentences, translations,
what the sound classifier heard, moments the operator marked) and, when chosen, audio.flac.
Folders older than KEEP_DAYS are removed.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

KEEP_DAYS = 30
SAMPLE_RATE = 16000


def prune(root: Path, keep_days: int = KEEP_DAYS) -> None:
    """Remove the logs of old services."""
    limit = time.time() - keep_days * 86400
    for folder in root.glob("*"):
        if folder.is_dir() and folder.stat().st_mtime < limit:
            log.info("Removing old service log %s", folder.name)
            shutil.rmtree(folder, ignore_errors=True)


class ServiceLog:
    def __init__(self, root: Path, audio: bool = False):
        prune(root)
        self.folder = root / time.strftime("%Y-%m-%d_%H-%M-%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self._lock = threading.Lock()
        self._events = open(self.folder / "events.jsonl", "a", encoding="utf-8")
        self._audio = None
        if audio:
            import soundfile as sf

            self._audio = sf.SoundFile(
                self.folder / "audio.flac", "w", samplerate=SAMPLE_RATE, channels=1, format="FLAC", subtype="PCM_16"
            )

    def event(self, kind: str, **fields) -> None:
        """Times are seconds from the start of the log, the same clock as audio.flac while not paused."""
        line = {"t": round(time.time() - self.started, 2), "clock": time.strftime("%H:%M:%S"), "kind": kind, **fields}
        with self._lock:
            if self._events.closed:
                return
            self._events.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._events.flush()

    def audio(self, chunk: np.ndarray) -> None:
        with self._lock:
            if self._audio is not None:
                self._audio.write(chunk)

    def close(self) -> None:
        with self._lock:
            self._events.close()
            if self._audio is not None:
                self._audio.close()
                self._audio = None
