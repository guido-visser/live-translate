"""Runs a private LibreTranslate next to the app and translates through it."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import httpx

from ..config import Settings
from ..native import ACCESS_VIOLATION, runtime_hint

log = logging.getLogger(__name__)

PIVOT = "en"  # Argos models mostly go through English
# Everything the Argos index offers. Only the chosen languages get loaded, so the server can't
# be asked for this list: it answers with what is loaded now.
CATALOG = (
    "ar az bg bn ca cs da de el en eo es et eu fa fi fr ga gl he hi hu id it ja ko ky lt lv ms "
    "nb nl pl pt ro ru sk sl sq sv sw th tl tr uk ur vi zh zt"
).split()
# Written once the environment is fully installed; an interrupted install leaves
# libretranslate.exe behind without it, and is then installed again.
INSTALLED_MARKER = ".livetranslate-installed"
MODELS_MARKER = ".livetranslate-models"  # the language codes models were fetched for


def _venv_exe(env: Path, name: str) -> Path:
    if sys.platform == "win32":
        return env / "Scripts" / f"{name}.exe"
    return env / "bin" / name


class LibreTranslate:
    def __init__(self, settings: Settings, data_dir: Path):
        self.data_dir = data_dir
        self.env_dir = data_dir / "libretranslate-env"
        self.log_path = data_dir / "libretranslate.log"
        self.status = "stopped"  # stopped|installing|starting|ready|error
        self.detail = ""
        self.languages: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._client = httpx.AsyncClient(timeout=30)
        self._cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = asyncio.Lock()
        self._loaded: tuple[str, ...] = ()
        self.configure(settings)

    def configure(self, settings: Settings) -> None:
        self.external_url = settings.libretranslate_url.rstrip("/")
        self.port = settings.libretranslate_port
        self.url = self.external_url or f"http://127.0.0.1:{self.port}"
        self.source = settings.source_language
        self.wanted = tuple(sorted({settings.source_language, PIVOT, *settings.target_languages}))

    @property
    def needs_restart(self) -> bool:
        return not self.external_url and self._loaded != self.wanted

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            try:
                await self._start()
            except Exception as exc:
                log.exception("LibreTranslate failed to start")
                self.status, self.detail = "error", str(exc)

    async def _start(self) -> None:
        if self.external_url:
            self.status, self.detail = "starting", self.external_url
            await self._wait_ready(timeout=30)
            return
        if await self._ping():
            # Probably left running by a previous session; reuse it.
            log.info("LibreTranslate already running on port %d", self.port)
            self._loaded = self.wanted
            await self._fetch_languages()
            self.status, self.detail = "ready", ""
            return
        exe = await self._ensure_installed()
        self.status, self.detail = "starting", "Loading language models (first start downloads them)…"
        args = [
            str(exe),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--load-only", ",".join(self.wanted),
            "--disable-web-ui",
        ]
        # By itself it only fetches models on its very first start, so a language added later wouldn't work.
        if set(self.wanted) - self._models_installed():
            args.append("--update-models")
        log.info("Starting %s", " ".join(args))
        log_file = open(self.log_path, "ab")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # UTF-8 output: LibreTranslate prints "→" and crashes on a cp1252 console.
        # PYTHONFAULTHANDLER: a crash in a native library still leaves a stack in the log.
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONFAULTHANDLER": "1",
            "ARGOS_DEVICE_TYPE": "cpu",
        }
        self._proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags, env=env)
        self._loaded = self.wanted
        await self._wait_ready(timeout=1800)

    def _models_installed(self) -> set[str]:
        try:
            return set((self.env_dir / MODELS_MARKER).read_text("utf-8").split())
        except OSError:
            return set()

    def _remember_models(self) -> None:
        """What the server says it has: without internet a new language isn't there, and gets another try."""
        if self.env_dir.exists():
            have = self._models_installed() | {lang["code"] for lang in self.languages}
            (self.env_dir / MODELS_MARKER).write_text(" ".join(sorted(have)), "utf-8")

    async def _ensure_installed(self) -> Path:
        # Always our own environment: a libretranslate found on PATH may belong to a
        # Python it doesn't run on (e.g. 3.14 breaks its spaCy/pydantic v1 imports).
        exe = _venv_exe(self.env_dir, "libretranslate")
        marker = self.env_dir / INSTALLED_MARKER
        if marker.exists() and exe.exists():
            return exe
        self.status, self.detail = "installing", "Installing LibreTranslate (one time, needs internet)…"
        log.info("Installing LibreTranslate into %s", self.env_dir)
        if self.env_dir.exists():
            # Left over from an install that didn't finish.
            await asyncio.to_thread(shutil.rmtree, self.env_dir, ignore_errors=True)
        uv = os.environ.get("LIVETRANSLATE_UV") or shutil.which("uv")
        if uv:
            cmds = [
                [uv, "venv", "--python", "3.11", str(self.env_dir)],
                [uv, "pip", "install", "--python", str(_venv_exe(self.env_dir, "python")), "libretranslate"],
            ]
        else:
            cmds = [
                [sys.executable, "-m", "venv", str(self.env_dir)],
                [str(_venv_exe(self.env_dir, "python")), "-m", "pip", "install", "libretranslate"],
            ]
        install_log = self.data_dir / "libretranslate-install.log"
        with open(install_log, "w", encoding="utf-8") as f:
            for cmd in cmds:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                out, _ = await proc.communicate()
                text = out.decode(errors="replace")
                f.write(f"> {' '.join(cmd)}\n{text}\n")
                if proc.returncode != 0:
                    await asyncio.to_thread(shutil.rmtree, self.env_dir, ignore_errors=True)
                    raise RuntimeError(f"{' '.join(cmd[:3])} failed: {_first_error(text)} (full output in {install_log})")
        marker.write_text("ok", "utf-8")
        return exe

    async def _ping(self) -> bool:
        try:
            r = await self._client.get(f"{self.url}/languages", timeout=2)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_ready(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(self._exit_message(self._proc.returncode))
            if await self._ping():
                await self._fetch_languages()
                self._remember_models()
                self.status, self.detail = "ready", ""
                log.info("LibreTranslate ready with %d languages", len(self.languages))
                return
            await asyncio.sleep(1)
        raise TimeoutError("LibreTranslate did not become ready in time")

    def _exit_message(self, code: int) -> str:
        if code in ACCESS_VIOLATION:
            message = "LibreTranslate crashed while loading (access violation)."
            hint = runtime_hint()
            if hint:
                return f"{message} {hint}"
        else:
            message = f"LibreTranslate exited with code {code}."
        tail = _tail(self.log_path)
        return f"{message} Last output: {tail}" if tail else f"{message} See {self.log_path}"

    async def _fetch_languages(self) -> None:
        r = await self._client.get(f"{self.url}/languages")
        r.raise_for_status()
        self.languages = r.json()

    async def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                await asyncio.to_thread(self._proc.wait, 10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.status = "stopped"

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def close(self) -> None:
        await self.stop()
        await self._client.aclose()

    # -- translation -----------------------------------------------------------

    @property
    def catalog(self) -> list[str]:
        """The languages that can be chosen, loaded or not."""
        return [lang["code"] for lang in self.languages] if self.external_url else list(CATALOG)

    def targets_for(self, source: str) -> list[str]:
        for lang in self.languages:
            if lang["code"] == source:
                return list(lang.get("targets", []))
        return []

    def name_of(self, code: str) -> str:
        for lang in self.languages:
            if lang["code"] == code:
                return lang["name"]
        return code

    async def translate(self, text: str, source: str, target: str) -> str:
        if source == target or not text:
            return text
        key = (text, source, target)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        r = await self._client.post(
            f"{self.url}/translate",
            json={"q": text, "source": source, "target": target, "format": "text"},
        )
        r.raise_for_status()
        result = r.json()["translatedText"]
        self._cache[key] = result
        if len(self._cache) > 2000:
            self._cache.popitem(last=False)
        return result


def _first_error(output: str) -> str:
    """The line that says what went wrong (uv prints "error: …"), else the last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith(("error", "x ", "×")) or "error:" in line.lower():
            return line[:300]
    return lines[-1][:300] if lines else "no output"


def _tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())[:400]
