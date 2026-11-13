import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from livetranslate import native
from livetranslate.audio.capture import LoopbackSource
from livetranslate.audio.devices import list_loopback_devices
from livetranslate.config import AudioSettings, Settings
from livetranslate.translate import libretranslate
from livetranslate.translate.libretranslate import LibreTranslate, _first_error
from livetranslate.tts.piper import VoiceBank

OLD_RUNTIME = (14, 32, 31332, 0)


def test_missing_module_is_reported_without_crashing():
    error = native._probe("no_such_module_here")
    assert error.startswith("no_such_module_here cannot be loaded (ModuleNotFoundError")


def test_crash_on_import_explains_the_old_runtime(monkeypatch):
    crashed = subprocess.CompletedProcess([], returncode=-1073741819, stdout="", stderr="")
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: crashed)
    monkeypatch.setattr(native, "vc_runtime_version", lambda: OLD_RUNTIME)
    error = native._probe("onnxruntime")
    assert "crashed with an access violation" in error
    assert "14.32.31332" in error and native.VC_REDIST_URL in error


def test_recent_runtime_gives_no_hint(monkeypatch):
    monkeypatch.setattr(native, "vc_runtime_version", lambda: (14, 44, 35211, 0))
    assert native.runtime_hint() == ""
    assert not native.vc_runtime_outdated()


def test_broken_onnxruntime_disables_voices_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "_onnx_error", "onnxruntime cannot be loaded (crashed).")
    bank = VoiceBank(tmp_path)
    assert not bank.has_voice("ru")
    with pytest.raises(RuntimeError, match="onnxruntime cannot be loaded"):
        bank.prepare("ru")


def test_pyaudio_never_starts_or_stops_on_two_threads_at_once(monkeypatch):
    """That crashes the whole app, and page reloads can ask for the device list at any moment."""
    busy = threading.Semaphore()
    overlaps = []

    def critical():
        if not busy.acquire(blocking=False):
            overlaps.append(True)
            return
        time.sleep(0.005)
        busy.release()

    device = {"name": "Speakers", "index": 0, "maxInputChannels": 2, "defaultSampleRate": 48000}

    class FakePyAudio:
        def __init__(self):
            critical()

        def terminate(self):
            critical()

        def get_default_wasapi_loopback(self):
            return device

        def get_loopback_device_info_generator(self):
            return iter([device])

        def open(self, **kwargs):
            return MagicMock()

    fake = SimpleNamespace(PyAudio=FakePyAudio, paFloat32=1, paContinue=0)
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake)
    monkeypatch.setattr(sys, "platform", "win32")

    def listing():
        for _ in range(10):
            list_loopback_devices()

    def capturing():
        for _ in range(10):
            source = LoopbackSource(AudioSettings())
            source.start(lambda block: None)
            source.stop()

    threads = [threading.Thread(target=job) for job in (listing, listing, capturing, capturing)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not overlaps


def test_libretranslate_crash_message(monkeypatch, tmp_path):
    lt = LibreTranslate(Settings(), tmp_path)
    monkeypatch.setattr(libretranslate, "runtime_hint", lambda: "Update the runtime.")
    assert lt._exit_message(3221225477) == "LibreTranslate crashed while loading (access violation). Update the runtime."
    lt.log_path.write_text("loading\nTraceback\nValueError: bad model\n", "utf-8")
    assert lt._exit_message(1) == "LibreTranslate exited with code 1. Last output: loading | Traceback | ValueError: bad model"


def test_install_error_shows_the_error_line_not_the_last_packages():
    output = "Resolved 80 packages\n  x Failed to build `sentencepiece==0.2.2`\nerror: build backend failed\n + tqdm==4.67.1\n"
    assert _first_error(output) == "x Failed to build `sentencepiece==0.2.2`"
    assert _first_error(" + tqdm==4.67.1\n") == "+ tqdm==4.67.1"


def test_gpu_sessions_never_run_at_the_same_moment():
    """Two DirectML sessions running at once crashed the NVIDIA driver and the app (13 Nov 2026)."""
    busy = threading.Semaphore()
    overlaps = []

    class FakeSession:
        def run(self, outputs, feeds):
            if not busy.acquire(blocking=False):
                overlaps.append(True)
            else:
                time.sleep(0.002)
                busy.release()
            return ["ok"]

        def get_inputs(self):
            return ["waveform"]

    sessions = [native.GpuSession(FakeSession(), name) for name in ("yamnet", "en", "uk")]

    def loop(session):
        for _ in range(30):
            assert session.run(None, {}) == ["ok"]

    threads = [threading.Thread(target=loop, args=(s,)) for s in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not overlaps
    assert sessions[0].get_inputs() == ["waveform"]  # everything else goes to the real session
