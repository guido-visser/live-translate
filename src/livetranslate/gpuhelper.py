"""The small GPU models (YAMNet, Piper voices) run in a helper process of their own.

On 13 Nov 2026 the NVIDIA driver crashed inside the app while two of these models ran on
DirectML at once, and took the whole app down mid-service. They now take turns (see
native.GPU_LOCK) and, as a second line, run in this separate process: when a driver or DLL
crashes it, only the helper dies. The app keeps translating text, the spoken audio goes quiet
and songs aren't skipped for a moment, and the helper starts again by itself, on the GPU (never
the CPU: next to vMix the CPU is what the livestream needs), and loads what it had loaded.
When it keeps crashing it stays off until translation is started again.

The helper handles one request at a time, so its models never run at the same moment. It talks
to the app over a named pipe with a random key, and ends when the app does.

    python -m livetranslate.gpuhelper <pipe address>      (key in LIVETRANSLATE_GPU_KEY)
"""

from __future__ import annotations

import atexit
import itertools
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .native import DIRECTML_OK_ENV

log = logging.getLogger(__name__)

KEY_ENV = "LIVETRANSLATE_GPU_KEY"
START_TIMEOUT_S = 60  # the process is up and answers
LOAD_TIMEOUT_S = 180  # loading a model (the first time also checks DirectML, up to 60 s)
RUN_TIMEOUT_S = 30  # one pass or sentence takes well under a second; longer means it hangs
RESTART_DELAY_S = 1
MAX_CRASHES = 3  # within CRASH_WINDOW_S; then it stays off until translation starts again
CRASH_WINDOW_S = 10 * 60
LOG_MAX_BYTES = 1_000_000


class HelperUnavailable(RuntimeError):
    """The helper isn't running right now (starting, starting again, or off after crashing)."""


class HelperCrashed(HelperUnavailable):
    """The helper died (or hung) during this request."""


class HelperError(RuntimeError):
    """The request failed with an ordinary error inside the helper (e.g. a broken voice file)."""


# -- inside the helper process ------------------------------------------------------------------


class Worker:
    """Holds the models and answers requests; runs in the helper process (tests run it in-process)."""

    def __init__(self) -> None:
        self.sound: dict[bool, object] = {}
        self.voices: dict[tuple[str, bool], object] = {}

    def handle(self, op: str, args: tuple):
        return getattr(self, "op_" + op)(*args)

    def op_ping(self) -> str:
        return "pong"

    def op_directml(self) -> bool | None:
        """Whether this helper found DirectML working (None: not checked yet)."""
        from . import native

        return native._directml

    def op_sound(self, gpu: bool) -> bool:
        if gpu not in self.sound:
            from .stt.sound import SoundClassifier

            self.sound[gpu] = SoundClassifier(gpu)
        return True

    def op_analyse(self, gpu: bool, audio: np.ndarray):
        self.op_sound(gpu)
        scores, description = self.sound[gpu].analyse(audio)
        return (scores.speech, scores.singing, scores.music), description

    def _load_voice(self, onnx: str, meta: str, gpu: bool):
        import json

        from piper import PiperVoice
        from piper.config import PiperConfig

        from .native import onnx_session, require_onnxruntime

        require_onnxruntime()
        # Not PiperVoice.load: that first opens the model on the CPU with every core, which is
        # slow and makes a livestream on this PC stutter, only to throw that session away.
        with open(meta, encoding="utf-8") as f:
            config = PiperConfig.from_dict(json.load(f))
        return PiperVoice(config=config, session=onnx_session(Path(onnx), gpu))

    def op_voice(self, onnx: str, meta: str, gpu: bool) -> bool:
        if (onnx, gpu) not in self.voices:
            self.voices[(onnx, gpu)] = self._load_voice(onnx, meta, gpu)
        return True

    def op_unload_voices(self) -> bool:
        self.voices.clear()
        return True

    def op_synthesize(self, onnx: str, meta: str, gpu: bool, text: str, length_scale: float) -> np.ndarray:
        """int16 mono audio at the stream's rate."""
        import soxr
        from piper import SynthesisConfig
        from piper.config import PhonemeType

        from .tts.piper import STREAM_RATE

        self.op_voice(onnx, meta, gpu)
        voice = self.voices[(onnx, gpu)]
        if voice.config.phoneme_type == PhonemeType.TEXT:
            text = text.lower()  # character-based voices (e.g. Ukrainian) only know lowercase letters
        config = SynthesisConfig(length_scale=length_scale)
        try:
            chunks = list(voice.synthesize(text, syn_config=config))
        except Exception:
            # E.g. the GPU ran out of memory. Load the voice again on the same processor, never
            # the CPU; when that fails too this sentence isn't spoken (it is still on screen).
            log.exception("Voice %s failed; loading it again", Path(onnx).stem)
            voice = self.voices[(onnx, gpu)] = self._load_voice(onnx, meta, gpu)
            chunks = list(voice.synthesize(text, syn_config=config))
        parts, rate = [], STREAM_RATE
        for chunk in chunks:
            parts.append(chunk.audio_float_array)
            rate = chunk.sample_rate
        if not parts:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(parts).astype(np.float32)
        if rate != STREAM_RATE:
            audio = soxr.resample(audio, rate, STREAM_RATE)
        return (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    # For tests: a native crash and a hang, as the GPU driver produces them.
    def op_crash(self) -> None:
        import faulthandler

        faulthandler._sigsegv()

    def op_hang(self, seconds: float) -> None:
        time.sleep(seconds)


def child_main(address: str) -> int:
    import faulthandler
    from multiprocessing.connection import Listener

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr
    )
    logging.getLogger("piper.phoneme_ids").setLevel(logging.ERROR)  # one line per unknown letter
    faulthandler.enable(sys.stderr, all_threads=True)
    key = bytes.fromhex(os.environ.pop(KEY_ENV))
    worker = Worker()
    with Listener(address, family="AF_PIPE", authkey=key) as listener:
        with listener.accept() as conn:
            log.info("GPU helper %d ready", os.getpid())
            while True:
                try:
                    op, args = conn.recv()
                except (EOFError, OSError):
                    return 0  # the app closed or ended
                try:
                    reply = ("ok", worker.handle(op, args))
                except Exception as exc:
                    log.exception("%s failed", op)
                    reply = ("error", f"{type(exc).__name__}: {exc}")
                conn.send(reply)


# -- in the app ---------------------------------------------------------------------------------


class GpuHelper:
    """Starts the helper, sends it requests (one at a time) and starts it again when it dies.

    call() raises HelperUnavailable at once while the helper isn't there, so a crash costs a
    sound check or a spoken sentence, never a stuck recognition thread."""

    def __init__(self, log_path: Path | None = None, command: list[str] | None = None):
        self.log_path = log_path
        self.command = command or [sys.executable, "-m", "livetranslate.gpuhelper"]
        self.state = "stopped"  # stopped|starting|ready|restarting|off
        self.pid: int | None = None
        self.restarts = 0
        self._proc: subprocess.Popen | None = None
        self._conn = None
        self._call_lock = threading.Lock()  # one request at a time
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._settled = threading.Event()  # ready or off: waiting makes no sense any more
        self._warm: dict[str, tuple[str, tuple]] = {}  # loaded again after a restart
        self._crashes: deque[float] = deque()
        self._names = itertools.count(1)
        self._directml_ok = False

    # -- life cycle --

    def ensure_started(self, timeout: float = START_TIMEOUT_S + LOAD_TIMEOUT_S) -> None:
        """Start the helper when it isn't running, and wait until it can take requests."""
        with self._state_lock:
            if self.state == "stopped":
                self._set_state("starting")
                threading.Thread(target=self._start, name="gpu-helper-start", daemon=True).start()
        self._settled.wait(timeout)
        if self.state != "ready":
            raise HelperUnavailable(f"the GPU helper is {self.state}")

    def enable(self) -> None:
        """Allow starting again after it was switched off for crashing too often (a new start of translation)."""
        with self._state_lock:
            if self.state == "off":
                self._crashes.clear()
                self._set_state("stopped")

    def stop(self) -> None:
        with self._state_lock:
            self._set_state("stopped")
            self._settled.set()
        self._close(kill_after_s=3)

    def remember(self, key: str, op: str, args: tuple) -> None:
        """Load this again whenever the helper starts again."""
        self._warm[key] = (op, args)

    def forget(self, prefix: str) -> None:
        for key in [k for k in self._warm if k.startswith(prefix)]:
            del self._warm[key]

    def _set_state(self, state: str) -> None:
        self.state = state
        if state == "ready":
            self._ready.set()
        else:
            self._ready.clear()
        if state in ("ready", "off"):
            self._settled.set()
        elif state in ("starting", "restarting"):
            self._settled.clear()

    def _spawn(self):
        from multiprocessing.connection import Client

        address = rf"\\.\pipe\livetranslate-gpu-{os.getpid()}-{next(self._names)}-{secrets.token_hex(4)}"
        key = secrets.token_bytes(32)
        env = {**os.environ, KEY_ENV: key.hex()}
        if self._directml_ok:
            env[DIRECTML_OK_ENV] = "1"
        out = subprocess.DEVNULL
        if self.log_path is not None:
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                    self.log_path.unlink()
                out = open(self.log_path, "ab")
            except OSError:
                out = subprocess.DEVNULL
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                [*self.command, address], stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, creationflags=flags
            )
        finally:
            if out is not subprocess.DEVNULL:
                out.close()  # the child has its own handle
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            try:
                return proc, Client(address, family="AF_PIPE", authkey=key)
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            except Exception:
                proc.kill()
                raise
            if proc.poll() is not None:
                raise HelperCrashed(f"the GPU helper ended while starting (exit code {_hex(proc.returncode)})")
            if time.monotonic() > deadline:
                proc.kill()
                raise HelperCrashed("the GPU helper didn't start in time")
            time.sleep(0.05)

    def _start(self) -> None:
        with self._call_lock:
            if self.state not in ("starting", "restarting"):
                return
            try:
                self._proc, self._conn = self._spawn()
                self.pid = self._proc.pid
                for key, (op, args) in list(self._warm.items()):
                    try:
                        self._request(op, args, LOAD_TIMEOUT_S)
                    except HelperError as exc:
                        log.error("The GPU helper could not load %s again: %s", key, exc)
                        self._warm.pop(key, None)
            except HelperUnavailable as exc:
                self._crashed(str(exc))
                return
            except Exception as exc:
                log.exception("Could not start the GPU helper")
                self._crashed(f"{type(exc).__name__}: {exc}")
                return
            if not self._directml_ok:
                self._directml_ok = self._request("directml", (), RUN_TIMEOUT_S) is True
            with self._state_lock:
                if self.state in ("starting", "restarting"):
                    log.info("GPU helper running (process %d)%s", self.pid, " again" if self.restarts else "")
                    self._set_state("ready")

    def _close(self, kill_after_s: float = 0.0) -> int | None:
        conn, proc = self._conn, self._proc
        self._conn = self._proc = None
        if conn is not None:
            try:
                conn.close()  # the helper ends by itself when the pipe closes
            except OSError:
                pass
        if proc is None:
            return None
        try:
            return proc.wait(kill_after_s) if kill_after_s else proc.wait(0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                return proc.wait(5)
            except subprocess.TimeoutExpired:
                return None

    def _crashed(self, reason: str) -> None:
        """Called with the call lock held: clean up, then start again later (or give up)."""
        if self.state in ("stopped", "off"):
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()  # hung
        code = self._close()
        now = time.monotonic()
        self._crashes.append(now)
        while self._crashes and now - self._crashes[0] > CRASH_WINDOW_S:
            self._crashes.popleft()
        log.error(
            "The GPU helper stopped (%s, exit code %s): spoken translations pause and songs aren't "
            "skipped until it is back; the text goes on",
            reason,
            _hex(code),
        )
        with self._state_lock:
            if len(self._crashes) >= MAX_CRASHES:
                log.error(
                    "The GPU helper crashed %d times in %d minutes; it stays off until translation is started again",
                    len(self._crashes),
                    CRASH_WINDOW_S // 60,
                )
                self._set_state("off")
                return
            self._set_state("restarting")
        self.restarts += 1
        timer = threading.Timer(RESTART_DELAY_S, self._start)
        timer.daemon = True
        timer.start()

    # -- requests --

    def _request(self, op: str, args: tuple, timeout: float):
        conn = self._conn
        try:
            conn.send((op, args))
            if not conn.poll(timeout):
                raise HelperCrashed(f"no answer to {op} within {timeout:.0f} s")
            status, value = conn.recv()
        except HelperCrashed:
            raise
        except (EOFError, OSError) as exc:
            raise HelperCrashed(f"{op}: {type(exc).__name__}") from exc
        if status == "error":
            raise HelperError(value)
        return value

    def call(self, op: str, *args, timeout: float = RUN_TIMEOUT_S, wait: bool = False):
        """Run one request. wait: start the helper / wait for its restart first (for loading models)."""
        if wait:
            self.ensure_started()
        elif self.state != "ready":
            raise HelperUnavailable(f"the GPU helper is {self.state}")
        with self._call_lock:
            if self.state != "ready":
                raise HelperUnavailable(f"the GPU helper is {self.state}")
            try:
                return self._request(op, args, timeout)
            except HelperCrashed as exc:
                self._crashed(str(exc))
                raise


def _hex(code: int | None) -> str:
    return "unknown" if code is None else f"0x{code & 0xFFFFFFFF:08X}"


_shared: GpuHelper | None = None
_shared_lock = threading.Lock()


def shared() -> GpuHelper:
    """The app's one helper."""
    global _shared
    with _shared_lock:
        if _shared is None:
            from .config import data_dir

            _shared = GpuHelper(data_dir() / "gpu-helper.log")
            atexit.register(_shared.stop)
        return _shared


class SoundClient:
    """The sound classifier (see stt/sound.py), running in the helper. Same analyse()."""

    def __init__(self, gpu: bool = True, helper: GpuHelper | None = None):
        self.helper = helper or shared()
        self.gpu = gpu
        self.helper.remember(f"sound:{gpu}", "sound", (gpu,))
        self.helper.call("sound", gpu, wait=True, timeout=LOAD_TIMEOUT_S)

    def analyse(self, audio: np.ndarray):
        from .stt.sound import SoundScores

        (speech, singing, music), description = self.helper.call("analyse", self.gpu, audio)
        return SoundScores(speech, singing, music), description

    def scores(self, audio: np.ndarray):
        return self.analyse(audio)[0]


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1]))
