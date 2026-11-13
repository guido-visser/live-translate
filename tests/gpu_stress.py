"""Stress test for the DirectML crash of 13 Nov 2026 (not run by pytest; needs a GPU and Piper voices).

The sound classifier and Piper voices run on DirectML from different threads. Running them at
the same moment crashed the NVIDIA driver with an access violation. This runs YAMNet and the
given voices in tight loops, one thread each, in a child process, so a crash shows up as an
exit code instead of taking this script down:

    python tests/gpu_stress.py --seconds 120 <voice.onnx> [<voice.onnx> ...]
    python tests/gpu_stress.py --seconds 120 --no-lock <voice.onnx> ...   # the 0.1.12 situation

    python tests/gpu_stress.py --seconds 120 --helper [--crash-every 20] <voice.onnx> ...

--helper runs the models the way the app does since 0.1.13: in the GPU helper process, from
several threads. --crash-every makes the helper crash now and then, to see how long the
spoken audio and the sound checks are away.

Run it on the church PC with a vMix stream going as well.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path


def worker(seconds: float, voices: list[str], use_lock: bool) -> None:
    import numpy as np
    from piper import PiperVoice

    from livetranslate import native
    from livetranslate.stt.sound import SoundClassifier

    sound = SoundClassifier(gpu=True)
    assert isinstance(sound.session, native.GpuSession), "DirectML is not available here"
    loaded = []
    for path in voices:
        voice = PiperVoice.load(path, config_path=path + ".json")
        voice.session = native.onnx_session(Path(path), gpu=True)
        loaded.append(voice)
    if not use_lock:  # straight to onnxruntime, as 0.1.12 did
        sound.session = sound.session._session
        for voice in loaded:
            voice.session = voice.session._session

    counts = [0] * (1 + len(loaded))
    end = time.monotonic() + seconds
    rng = np.random.default_rng(0)
    audio = rng.standard_normal(32000).astype(np.float32) * 0.1

    def classify():
        while time.monotonic() < end:
            sound.analyse(audio)
            counts[0] += 1

    def speak(i, voice):
        ids = [1] + [int(x) for x in rng.integers(2, 60, 120)] + [2]  # about a sentence
        while time.monotonic() < end:
            voice.phoneme_ids_to_audio(ids)
            counts[i] += 1

    threads = [threading.Thread(target=classify)]
    threads += [threading.Thread(target=speak, args=(i + 1, v)) for i, v in enumerate(loaded)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    waits = [round(s.longest_wait_s, 3) for s in [sound.session, *(v.session for v in loaded)] if hasattr(s, "longest_wait_s")]
    print(f"runs per thread: {counts}; longest wait for the GPU (s): {waits}", flush=True)


def through_helper(seconds: float, voices: list[str], crash_every: float) -> int:
    import numpy as np

    from livetranslate.gpuhelper import GpuHelper, HelperUnavailable, SoundClient

    helper = GpuHelper(Path("gpu-stress-helper.log").resolve())
    sound = SoundClient(gpu=True, helper=helper)
    for path in voices:
        helper.remember(path, "voice", (path, path + ".json", True))
        helper.call("voice", path, path + ".json", True, wait=True, timeout=180)
    end = time.monotonic() + seconds
    audio = np.random.default_rng(0).standard_normal(32000).astype(np.float32) * 0.1
    counts = [0] * (1 + len(voices))
    missed = [0] * (1 + len(voices))
    gaps: list[float] = []
    lock = threading.Lock()

    def loop(i, job):
        while time.monotonic() < end:
            try:
                job()
                counts[i] += 1
            except HelperUnavailable:
                missed[i] += 1
                time.sleep(0.2)

    def crasher():
        while time.monotonic() + crash_every < end:
            time.sleep(crash_every)
            try:
                helper.call("crash")
            except HelperUnavailable:
                pass
            down = time.monotonic()
            while helper.state != "ready" and time.monotonic() < end:
                time.sleep(0.05)
            with lock:
                gaps.append(time.monotonic() - down)

    jobs = [lambda: sound.analyse(audio)]
    jobs += [lambda p=p: helper.call("synthesize", p, p + ".json", True, "Dit is een zin om te testen hoe lang het duurt.", 1.0) for p in voices]
    threads = [threading.Thread(target=loop, args=(i, j)) for i, j in enumerate(jobs)]
    if crash_every:
        threads.append(threading.Thread(target=crasher))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    helper.stop()
    print(f"runs per thread: {counts}; missed while the helper was away: {missed}")
    if gaps:
        print(f"helper crashes: {len(gaps)}; back after (s): {[round(g, 1) for g in gaps]}; state: {helper.state}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("voices", nargs="+")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--helper", action="store_true")
    parser.add_argument("--crash-every", type=float, default=0)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.helper:
        return through_helper(args.seconds, args.voices, args.crash_every)
    if args.child:
        worker(args.seconds, args.voices, not args.no_lock)
        return 0
    command = [sys.executable, __file__, "--child", "--seconds", str(args.seconds), *args.voices]
    if args.no_lock:
        command.append("--no-lock")
    started = time.monotonic()
    code = subprocess.call(command)
    took = time.monotonic() - started
    if code == 0:
        print(f"Survived {took:.0f} s ({'without' if args.no_lock else 'with'} the GPU lock)")
    else:
        print(f"CRASHED after {took:.0f} s with exit code 0x{code & 0xFFFFFFFF:08X}")
    return code


if __name__ == "__main__":
    sys.exit(main())
