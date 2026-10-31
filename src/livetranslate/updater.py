"""Updates from GitHub Releases: check for a newer version, download and run its installer.

Only the installed Windows app installs updates itself; elsewhere the operator page just
says a new version exists. Installing is never automatic: the operator starts it, and only
while nothing is being translated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger(__name__)

REPO = "guido-visser/live-translate"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY_S = 6 * 3600
FIRST_CHECK_AFTER_S = 15


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.1.10' → (0, 1, 10); anything after the numbers (e.g. '-rc1') is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def install_dir() -> Path | None:
    """Folder of the Windows installation, or None when running from source.

    The launcher (LiveTranslate.cmd) sets LIVETRANSLATE_UV to <install dir>\\bin\\uv.exe."""
    uv = os.environ.get("LIVETRANSLATE_UV")
    if sys.platform != "win32" or not uv:
        return None
    folder = Path(uv).resolve().parent.parent
    return folder if (folder / "VERSION").exists() else None


class Updater:
    def __init__(self, data: Path, is_idle: Callable[[], bool], request_exit: Callable[[], None] | None = None):
        self.folder = data / "updates"
        self.is_idle = is_idle  # True while nothing is being translated
        self.request_exit = request_exit  # set by __main__: closes the app so the installer can run
        self.current = __version__
        self.can_install = install_dir() is not None
        self.latest: dict | None = None  # {"version", "url", "asset", "size", "sha256"}
        self.state = "idle"  # idle | checking | downloading | installing | error
        self.progress = 0.0
        self.error = ""
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self.latest) and parse_version(self.latest["version"]) > parse_version(self.current)

    def status(self) -> dict:
        latest = self.latest or {}
        return {
            "current": self.current,
            "latest": latest.get("version"),
            "url": latest.get("url"),
            "available": self.available,
            "can_install": self.can_install and bool(latest.get("asset")),
            "state": self.state,
            "progress": round(self.progress, 3),
            "error": self.error,
        }

    async def check(self) -> dict:
        if self._lock.locked():
            return self.status()
        async with self._lock:
            self.state, self.error = "checking", ""
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    r = await client.get(LATEST_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "LiveTranslate"})
                    r.raise_for_status()
                self.latest = release_info(r.json())
                self.state = "idle"
                if self.available:
                    log.info("Update available: %s (running %s)", self.latest["version"], self.current)
            except Exception as exc:
                # Usually just no internet (the church PC often has none); keep what we knew.
                log.info("Update check failed: %s", exc)
                self.state, self.error = "error", str(exc) or type(exc).__name__
        return self.status()

    async def run_checks(self, enabled: Callable[[], bool]) -> None:
        """Background task: check shortly after start, then every few hours."""
        await asyncio.sleep(FIRST_CHECK_AFTER_S)
        while True:
            if enabled() and self.state not in ("downloading", "installing"):
                await self.check()
            await asyncio.sleep(CHECK_EVERY_S)

    async def install(self) -> None:
        """Download the installer, start it and close the app. Raises RuntimeError when not possible."""
        if not (self.can_install and self.available and self.latest.get("asset")):
            raise RuntimeError("No update to install")
        if not self.is_idle():
            raise RuntimeError("Stop translating before updating")
        if self._lock.locked():
            raise RuntimeError("An update is already in progress")
        async with self._lock:
            self.state, self.error, self.progress = "downloading", "", 0.0
            try:
                path = await self._download()
                self.state = "installing"
                await asyncio.to_thread(run_installer, path)
            except Exception as exc:
                log.exception("Update failed")
                self.state, self.error = "error", str(exc) or type(exc).__name__
                raise RuntimeError(self.error) from exc
        log.info("Installer for %s started; closing so it can replace the app", self.latest["version"])
        if self.request_exit:
            # A moment for the operator page to show "installing" before the server goes away.
            asyncio.get_running_loop().call_later(1.0, self.request_exit)

    async def _download(self) -> Path:
        info = self.latest
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.glob("*.exe"):
            old.unlink(missing_ok=True)
        target = self.folder / re.sub(r"[^\w.\-]", "_", info["asset"])
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", info["download"], headers={"User-Agent": "LiveTranslate"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or info.get("size") or 0)
                done = 0
                with open(partial, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if total:
                            self.progress = min(done / total, 1.0)
        if info.get("sha256") and digest.hexdigest() != info["sha256"]:
            partial.unlink(missing_ok=True)
            raise RuntimeError("The downloaded installer is damaged (checksum mismatch)")
        partial.replace(target)
        self.progress = 1.0
        return target


def release_info(release: dict) -> dict:
    """The parts of a GitHub release we need; 'asset' is None when it has no Windows installer."""
    info = {
        "version": str(release.get("tag_name", "")).lstrip("v"),
        "url": release.get("html_url"),
        "asset": None,
        "download": None,
        "size": 0,
        "sha256": "",
    }
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith("LiveTranslate-Setup-") and name.endswith(".exe"):
            digest = asset.get("digest") or ""
            info.update(
                asset=name,
                download=asset.get("browser_download_url"),
                size=asset.get("size", 0),
                sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
            )
            break
    return info


def run_installer(path: Path) -> None:
    """Start the installer elevated (Windows asks for permission) without waiting for it.

    /SILENT shows only a progress window; /relaunch=1 starts the app again afterwards and
    /waitpid makes Setup wait until this process has closed. Raises OSError when the
    operator declines the Windows permission prompt."""
    args = f'/SILENT /SUPPRESSMSGBOXES /NORESTART /relaunch=1 /waitpid={os.getpid()}'
    os.startfile(str(path), "open", args)  # type: ignore[attr-defined]  # Windows only
