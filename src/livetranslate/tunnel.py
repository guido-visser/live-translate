"""Listeners over the internet: an SSH reverse tunnel to a server of your own.

The server (e.g. nginx with HTTPS on a VPS) serves the public address and passes every
request to a port on its own 127.0.0.1, which this tunnel connects to this PC. Phones then
work over 5G as well as the church Wi-Fi, and the church router needs no changes.

The tunnel ends on a separate local port (Remote.local_port) that only serves the listener
pages: the operator page is refused there, however the server is configured. The app uses
its own key pair (no passwords); the public key goes into the server's authorized_keys.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
from collections import deque
from pathlib import Path

import httpx

from .config import RemoteSettings

log = logging.getLogger(__name__)

CONNECTED_AFTER_S = 5.0  # ssh exits within this when the login or the forwarding fails
HEALTH_EVERY_S = 60.0
RETRY_S = (5, 10, 30, 60)


def ssh_executable(name: str = "ssh") -> str | None:
    """The OpenSSH client (built into Windows 10/11)."""
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        builtin = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe"
        if builtin.exists():
            return str(builtin)
    return None


def explain(output: str, remote: RemoteSettings) -> str:
    """Turn ssh's last words into something the operator can act on."""
    lines = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("Warning: Permanently")]
    last = lines[-1] if lines else "the connection closed"
    text = output.lower()
    if "permission denied" in text:
        return f"The server refused this PC's key. Add the key below to ~/.ssh/authorized_keys of {remote.ssh_user or 'the user'} on the server."
    if "remote port forwarding failed" in text or "forwarding failed" in text:
        return f"Port {remote.remote_port} on the server is in use or not allowed for this key."
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return "The server's identity changed since the first connection. If that is expected, remove it with Forget server."
    return last


class Tunnel:
    def __init__(self, data: Path, local_port: int):
        self.key = data / "tunnel_key"
        self.known_hosts = data / "tunnel_known_hosts"
        self.local_port = local_port  # bound by __main__ for the life of the process
        self.settings = RemoteSettings()
        self.state = "off"  # off | connecting | connected | error
        self.error = ""
        self.reachable: bool | None = None  # the public address answers (checked while connected)
        self.reach_error = ""
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    # -- key -------------------------------------------------------------------

    def public_key(self) -> str:
        pub = self.key.with_name(self.key.name + ".pub")
        try:
            return pub.read_text("utf-8").strip()
        except OSError:
            return ""

    async def create_key(self) -> str:
        if self.public_key():
            return self.public_key()
        keygen = ssh_executable("ssh-keygen")
        if keygen is None:
            raise RuntimeError(NO_SSH)
        comment = f"livetranslate@{socket.gethostname()}"
        proc = await asyncio.create_subprocess_exec(
            keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(self.key),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
            creationflags=_no_window(),
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0 or not self.public_key():
            raise RuntimeError(f"ssh-keygen failed: {out.decode(errors='replace').strip()}")
        log.info("Created the tunnel key %s", self.key)
        return self.public_key()

    def forget_server(self) -> None:
        self.known_hosts.unlink(missing_ok=True)

    # -- status ----------------------------------------------------------------

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "reachable": self.reachable,
            "reach_error": self.reach_error,
            "public_key": self.public_key(),
            "local_port": self.local_port,
        }

    def command(self) -> list[str]:
        s = self.settings
        return [
            ssh_executable() or "ssh",
            "-N", "-T",
            "-i", str(self.key),
            "-p", str(s.ssh_port),
            "-R", f"127.0.0.1:{s.remote_port}:127.0.0.1:{self.local_port}",
            "-o", "BatchMode=yes",  # never wait for a password
            "-o", "IdentitiesOnly=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new",  # trust the server on first use, then pin it
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            f"{s.ssh_user}@{s.ssh_host}",
        ]

    # -- lifecycle -------------------------------------------------------------

    async def apply(self, settings: RemoteSettings) -> None:
        """Start, restart or stop the tunnel to match the settings."""
        if settings == self.settings and (self._task is not None) == settings.enabled:
            return
        await self.stop()
        self.settings = settings.model_copy()
        if settings.enabled:
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._kill()
        self.state, self.error, self.reachable, self.reach_error = "off", "", None, ""

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()

    async def _supervise(self) -> None:
        failures = 0
        while True:
            problem = self._config_problem()
            if problem:
                self.state, self.error = "error", problem
                return
            if not self.public_key():
                try:
                    await self.create_key()
                except RuntimeError as exc:
                    self.state, self.error = "error", str(exc)
                    return
            ok = await self._run_once()
            failures = 0 if ok else failures + 1
            await asyncio.sleep(RETRY_S[min(failures, len(RETRY_S) - 1)])

    def _config_problem(self) -> str:
        s = self.settings
        if not (s.ssh_host and s.ssh_user):
            return "Fill in the server and the user."
        if not self.local_port:
            return "The tunnel port on this PC could not be opened; restart Live Translate."
        if ssh_executable() is None:
            return NO_SSH
        return ""

    async def _run_once(self) -> bool:
        """Run ssh until it exits; True when it was connected for a while."""
        self.state, self.reachable, self.reach_error = "connecting", None, ""
        cmd = self.command()
        log.info("Opening the tunnel to %s@%s:%d", self.settings.ssh_user, self.settings.ssh_host, self.settings.ssh_port)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, creationflags=_no_window(),
            )
        except OSError as exc:
            self.state, self.error = "error", str(exc)
            return False
        proc = self._proc
        output: deque[str] = deque(maxlen=20)
        reader = asyncio.create_task(_collect(proc.stderr, output))
        health: asyncio.Task | None = None
        connected = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), CONNECTED_AFTER_S)
            except asyncio.TimeoutError:
                connected = True
                self.state, self.error = "connected", ""
                log.info("Tunnel open: %s", self.settings.public_url or f"server port {self.settings.remote_port}")
                health = asyncio.create_task(self._check_health())
                await proc.wait()
            await reader
        finally:
            if health is not None:
                health.cancel()
            reader.cancel()
            await self._kill()
        self.state, self.error, self.reachable = "error", explain("\n".join(output), self.settings), None
        log.warning("Tunnel closed: %s", self.error)
        return connected

    async def _check_health(self) -> None:
        url = self.settings.public_url.strip().rstrip("/")
        if not url:
            return
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            while True:
                try:
                    r = await client.get(f"{url}/api/public", headers={"Cache-Control": "no-cache"})
                    r.raise_for_status()
                    r.json()["languages"]
                    self.reachable, self.reach_error = True, ""
                except Exception as exc:
                    self.reachable = False
                    self.reach_error = f"{url} does not reach this PC ({_short(exc)}). Check the web server settings."
                await asyncio.sleep(HEALTH_EVERY_S)


NO_SSH = "The OpenSSH client is missing (Windows: Settings → System → Optional features → OpenSSH Client)."


async def _collect(stream: asyncio.StreamReader | None, into: deque[str]) -> None:
    if stream is None:
        return
    async for line in stream:
        into.append(line.decode(errors="replace").rstrip())


def _short(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return str(exc) or type(exc).__name__


def _no_window() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
