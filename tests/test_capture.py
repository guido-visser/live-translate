import types

import numpy as np

from livetranslate.audio.capture import DeviceSource
from livetranslate.config import AudioSettings
from livetranslate.pipeline import NO_SIGNAL_S, Pipeline


class FakeDevice(DeviceSource):
    """A capture device without sounddevice: counts how often it was opened."""

    def __init__(self):
        super().__init__(AudioSettings())
        self.opened = 0
        self.fail = False

    def start(self, callback):
        self._callback = callback
        self.opened += 1
        if self.fail:
            raise LookupError("Audio device not found")

    def stop(self):
        pass


def test_hiss_counts_as_signal_but_digital_silence_does_not(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("livetranslate.audio.capture.time.monotonic", lambda: now["t"])
    device = FakeDevice()
    device.start(lambda chunk: None)
    now["t"] += 30
    device._deliver(np.zeros((800, 2), dtype=np.float32))  # a USB interface that dropped out
    assert device.silent_s() == 30
    device._deliver(np.full((800, 2), 1e-5, dtype=np.float32))  # an open input, nobody talking
    assert device.silent_s() == 0


class FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def make_pipeline():
    p = types.SimpleNamespace(no_signal=False, events=[], broadcasts=0, _loop=FakeLoop())
    p._log = lambda kind, **fields: p.events.append(kind)

    def broadcast():
        p.broadcasts += 1

    p._broadcast_status = broadcast
    return p


def test_a_device_without_any_signal_for_a_minute_is_opened_again(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("livetranslate.audio.capture.time.monotonic", lambda: now["t"])
    device, p = FakeDevice(), make_pipeline()
    device.start(lambda chunk: None)
    now["t"] += NO_SIGNAL_S - 1
    Pipeline._check_signal(p, device)
    assert device.opened == 1 and not p.no_signal  # not yet: a minute of nothing at all
    now["t"] += 1
    Pipeline._check_signal(p, device)
    assert device.opened == 2 and p.no_signal and p.events == ["audio_reopened"]
    Pipeline._check_signal(p, device)
    assert device.opened == 2  # the next try is a minute later
    # The cable is back: the warning goes away.
    device._deliver(np.full((800, 1), 0.01, dtype=np.float32))
    Pipeline._check_signal(p, device)
    assert not p.no_signal and p.broadcasts == 2


def test_a_device_that_is_still_gone_is_tried_again_every_minute(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("livetranslate.audio.capture.time.monotonic", lambda: now["t"])
    device, p = FakeDevice(), make_pipeline()
    device.start(lambda chunk: None)
    device.fail = True
    for _ in range(3):
        now["t"] += NO_SIGNAL_S
        Pipeline._check_signal(p, device)  # the failure is logged, not raised
    assert device.opened == 4 and p.no_signal


def test_only_capture_devices_are_watched():
    p = make_pipeline()
    Pipeline._check_signal(p, None)
    Pipeline._check_signal(p, types.SimpleNamespace(silent_s=lambda: 10_000))  # loopback or file
    assert p.events == []
