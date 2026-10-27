"""Audio sources. Every source delivers 16 kHz mono float32 blocks to a callback."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soxr

from ..config import AudioSettings
from .devices import resolve_input_device, same_device_other_apis

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

AudioCallback = Callable[[np.ndarray], None]


class AudioSource:
    """Base class: channel selection, gain, resampling and the level meter."""

    def __init__(self, settings: AudioSettings):
        self.channel = settings.channel
        self.gain = settings.gain
        self.level_db = -120.0
        self.error: str | None = None
        self._callback: AudioCallback | None = None
        self._resampler: soxr.ResampleStream | None = None
        self._in_rate = SAMPLE_RATE

    def _setup(self, in_rate: int) -> None:
        self._in_rate = in_rate
        if in_rate != SAMPLE_RATE:
            self._resampler = soxr.ResampleStream(in_rate, SAMPLE_RATE, 1, dtype="float32")

    def _deliver(self, block: np.ndarray) -> None:
        """block: (frames, channels) float32 at the input rate."""
        if block.ndim == 1:
            mono = block
        elif self.channel and self.channel <= block.shape[1]:
            mono = block[:, self.channel - 1]
        else:
            mono = block.mean(axis=1)
        mono = np.ascontiguousarray(mono, dtype=np.float32)
        if self.gain != 1.0:
            mono = np.clip(mono * self.gain, -1.0, 1.0)
        if mono.size:
            rms = float(np.sqrt(np.mean(mono * mono)))
            db = 20 * math.log10(max(rms, 1e-6))
            # Fast attack, slow release, like a hardware meter.
            self.level_db = db if db > self.level_db else self.level_db * 0.8 + db * 0.2
        if self._resampler is not None:
            mono = self._resampler.resample_chunk(mono)
        if mono.size and self._callback is not None:
            self._callback(mono)

    def start(self, callback: AudioCallback) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class DeviceSource(AudioSource):
    """A capture device: audio interface, microphone, line-in, monitor source."""

    def __init__(self, settings: AudioSettings):
        super().__init__(settings)
        self.device_id = settings.device
        self._stream = None

    def start(self, callback: AudioCallback) -> None:
        import sounddevice as sd

        self._callback = callback
        first = resolve_input_device(self.device_id)
        candidates = [first, *same_device_other_apis(self.device_id)]
        for attempt, index in enumerate(candidates):
            try:
                self._open(sd, index)
                return
            except Exception as exc:
                if attempt == len(candidates) - 1:
                    raise
                log.warning("Could not open audio device %s (%s); trying another driver", index, exc)

    def _open(self, sd, index: int | None) -> None:
        info = sd.query_devices(index if index is not None else sd.default.device[0])
        channels = int(info["max_input_channels"])
        rate = int(info["default_samplerate"])
        self._setup(rate)

        def on_block(indata, frames, time_info, status):
            if status:
                log.debug("Audio status: %s", status)
            self._deliver(indata.copy())

        stream = sd.InputStream(
            device=index,
            channels=channels,
            samplerate=rate,
            dtype="float32",
            blocksize=int(rate * 0.05),
            callback=on_block,
        )
        stream.start()
        self._stream = stream
        log.info("Capturing from %s (%d ch @ %d Hz)", info["name"], channels, rate)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class LoopbackSource(AudioSource):
    """Windows WASAPI loopback: record what a playback device is playing."""

    def __init__(self, settings: AudioSettings):
        super().__init__(settings)
        self.device_id = settings.device
        self._pa = None
        self._stream = None

    def start(self, callback: AudioCallback) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "System audio capture is only built in on Windows. On macOS use BlackHole, "
                "on Linux pick the 'Monitor of …' input device."
            )
        import pyaudiowpatch as pyaudio

        self._callback = callback
        self._pa = pyaudio.PyAudio()
        name = self.device_id.split("::", 1)[1] if self.device_id else None
        dev = None
        for candidate in self._pa.get_loopback_device_info_generator():
            if name is None or candidate["name"] == name:
                dev = candidate
                break
        if dev is None:
            dev = self._pa.get_default_wasapi_loopback()
        channels = int(dev["maxInputChannels"])
        rate = int(dev["defaultSampleRate"])
        self._setup(rate)

        def on_block(in_data, frame_count, time_info, status):
            block = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
            self._deliver(block)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(rate * 0.05),
            stream_callback=on_block,
        )
        self._stream.start_stream()
        log.info("Capturing loopback from %s", dev["name"])

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


class FileSource(AudioSource):
    """Play an audio file in real time, for rehearsals and testing."""

    def __init__(self, settings: AudioSettings, realtime: bool = True):
        super().__init__(settings)
        self.path = Path(settings.file_path.strip().strip('"'))
        self.realtime = realtime
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, callback: AudioCallback) -> None:
        import soundfile as sf

        if not self.path.is_file():
            raise FileNotFoundError(f"Audio file not found: {self.path}")
        self._callback = callback
        self._file = sf.SoundFile(str(self.path))
        self._setup(self._file.samplerate)
        self._thread = threading.Thread(target=self._run, name="file-source", daemon=True)
        self._thread.start()
        log.info("Playing file %s", self.path)

    def _run(self) -> None:
        block_frames = int(self._file.samplerate * 0.1)
        started = time.monotonic()
        played = 0.0
        try:
            while not self._stop.is_set():
                block = self._file.read(block_frames, dtype="float32", always_2d=True)
                if not len(block):
                    break
                self._deliver(block)
                played += len(block) / self._file.samplerate
                if self.realtime:
                    delay = started + played - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            # Trailing silence so the last sentence gets finalized.
            if not self._stop.is_set() and self._callback is not None:
                self._callback(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))
        except Exception as exc:  # pragma: no cover - surfaced in the admin page
            log.exception("File playback failed")
            self.error = str(exc)
        finally:
            self._file.close()
            self.level_db = -120.0
            self.finished.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


def create_source(settings: AudioSettings) -> AudioSource:
    if settings.kind == "file":
        return FileSource(settings)
    if settings.kind == "loopback":
        return LoopbackSource(settings)
    return DeviceSource(settings)
