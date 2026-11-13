"""The GPU helper process: a crash or hang in it costs spoken audio for a moment, never the app."""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from livetranslate import gpuhelper
from livetranslate.gpuhelper import GpuHelper, HelperCrashed, HelperError, HelperUnavailable, Worker
from livetranslate.tts.piper import VoiceBank


@pytest.fixture
def helper(monkeypatch, tmp_path):
    monkeypatch.setattr(gpuhelper, "RESTART_DELAY_S", 0.1)
    h = GpuHelper(tmp_path / "gpu-helper.log")
    yield h
    h.stop()


def wait_for(condition, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_requests_go_to_a_separate_process(helper):
    assert helper.call("ping", wait=True) == "pong"
    assert helper.state == "ready" and helper.pid
    with pytest.raises(HelperError, match="no_such_op|AttributeError"):
        helper.call("no_such_op")
    assert helper.call("ping") == "pong"  # an ordinary error leaves it running


def test_a_native_crash_only_costs_a_moment(helper):
    helper.remember("warm", "ping", ())
    helper.call("ping", wait=True)
    first = helper.pid
    with pytest.raises(HelperCrashed):
        helper.call("crash")
    with pytest.raises(HelperUnavailable):  # straight away, no waiting on the recognition thread
        helper.call("ping")
    assert wait_for(lambda: helper.state == "ready")
    assert helper.pid != first and helper.restarts == 1
    assert helper.call("ping") == "pong"


def test_a_hang_is_cut_off(helper):
    helper.call("ping", wait=True)
    started = time.monotonic()
    with pytest.raises(HelperCrashed, match="no answer"):
        helper.call("hang", 60, timeout=1)
    assert time.monotonic() - started < 10
    assert wait_for(lambda: helper.state == "ready")


def test_after_crashing_too_often_it_stays_off_until_enabled(helper, monkeypatch):
    monkeypatch.setattr(gpuhelper, "MAX_CRASHES", 2)
    helper.call("ping", wait=True)
    with pytest.raises(HelperCrashed):
        helper.call("crash")
    assert wait_for(lambda: helper.state == "ready")
    with pytest.raises(HelperCrashed):
        helper.call("crash")
    assert helper.state == "off"
    with pytest.raises(HelperUnavailable):
        helper.call("ping", wait=True)
    helper.enable()
    assert helper.call("ping", wait=True) == "pong"


class FakeHelper:
    def __init__(self, error=None):
        self.error = error
        self.warm = {}

    def remember(self, key, op, args):
        self.warm[key] = (op, args)

    def forget(self, prefix):
        self.warm = {k: v for k, v in self.warm.items() if not k.startswith(prefix)}

    def call(self, op, *args, **kwargs):
        if self.error:
            raise self.error
        return np.ones(3, dtype=np.int16) if op == "synthesize" else True


def make_bank(tmp_path, helper):
    bank = VoiceBank(tmp_path, helper)
    for path in bank._local("en"):
        path.write_bytes(b"x")
    return bank


def test_a_broken_voice_is_given_up_but_a_crash_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr("livetranslate.tts.piper.require_onnxruntime", lambda: None)
    bank = make_bank(tmp_path, FakeHelper(HelperError("bad file")))
    with pytest.raises(HelperError):
        bank.prepare("en")
    assert not bank.has_voice("en") and not bank.helper.warm

    bank = make_bank(tmp_path, FakeHelper(HelperCrashed("driver")))
    with pytest.raises(HelperUnavailable):
        bank.prepare("en")
    assert bank.has_voice("en") and "voice:en" in bank.helper.warm  # loaded again after the restart

    bank = make_bank(tmp_path, FakeHelper())
    assert len(bank.synthesize("en", "Hello")) == 3


def test_a_failing_voice_loads_again_on_the_gpu_not_the_cpu(monkeypatch):
    loads = []

    class Voice:
        config = SimpleNamespace(phoneme_type=None)
        calls = 0

        def synthesize(self, text, syn_config=None):
            Voice.calls += 1
            if Voice.calls == 1:
                raise RuntimeError("GPU memory")
            return []

    worker = Worker()
    monkeypatch.setattr(worker, "_load_voice", lambda onnx, meta, gpu: loads.append(gpu) or Voice())
    assert len(worker.op_synthesize("v.onnx", "v.json", True, "Hello", 1.0)) == 0
    assert loads == [True, True]  # loaded, failed, loaded again: on the GPU both times
