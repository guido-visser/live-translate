"""Signing in to the admin page from other devices on the network (Settings → Web Server).

On this PC itself the admin page never asks for anything. Other devices sign in with a password
once and then carry a session cookie. Only hashes are stored: the password as scrypt in the
settings, the cookies as SHA-256 in sessions.json, so neither file lets anyone in when copied.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MIN_PASSWORD = 8
SESSION_DAYS = 30
SESSION_S = SESSION_DAYS * 24 * 3600
COOKIE = "lt_admin"
# last_seen is saved at most this often per session: a page polls the state all the time.
TOUCH_SAVE_S = 300
# More signed-in devices than a church has tablets: the oldest is signed out first.
MAX_SESSIONS = 50

_N, _R, _P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    """The signed-in devices; a session lasts SESSION_DAYS after it was last used.

    A session only works from the address it signed in from: a cookie overheard on the
    Wi-Fi (the page is plain HTTP) is no use from another device. A device that gets a new
    address signs in again."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}  # token hash → session
        self._saved: dict[str, float] = {}  # token hash → last_seen as last saved
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("Could not read %s; every device signs in again", self.path, exc_info=True)
            return
        now = self.clock()
        for key, s in (data.get("sessions") or {}).items():
            try:
                if now - float(s["last_seen"]) < SESSION_S:
                    self._sessions[key] = s
                    self._saved[key] = float(s["last_seen"])
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": self._sessions}, indent=2), "utf-8")
            tmp.replace(self.path)
            self._saved = {k: s["last_seen"] for k, s in self._sessions.items()}
        except OSError:
            log.warning("Could not save %s", self.path, exc_info=True)

    def create(self, ip: str, user_agent: str) -> tuple[str, str]:
        """A new session: (cookie token, session id)."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = {
            "id": secrets.token_hex(6),
            "created": now,
            "last_seen": now,
            "ip": ip[:64],
            "ua": user_agent[:300],
        }
        with self._lock:
            self._sessions[_token_hash(token)] = session
            while len(self._sessions) > MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["last_seen"])
                del self._sessions[oldest]
            self._save()
        return token, session["id"]

    def check(self, token: str | None, ip: str | None = None) -> str | None:
        """The session id of a valid cookie (and it counts as used now), or None.

        ip: the address the request comes from, which must be the one the session signed in
        from. None skips that check (signing out, where any copy of the cookie may do it)."""
        if not token:
            return None
        key = _token_hash(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if ip is not None and ip[:64] != session["ip"]:
                return None
            now = self.clock()
            if now - session["last_seen"] >= SESSION_S:
                del self._sessions[key]
                self._save()
                return None
            session["last_seen"] = now
            if now - self._saved.get(key, 0) >= TOUCH_SAVE_S:
                self._save()
            return session["id"]

    def list(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            rows = [dict(s) for s in self._sessions.values() if now - s["last_seen"] < SESSION_S]
        return sorted(rows, key=lambda s: s["last_seen"], reverse=True)

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s["id"] == session_id]
            for k in keys:
                del self._sessions[k]
            if keys:
                self._save()
            return bool(keys)

    def revoke_all(self) -> None:
        with self._lock:
            if self._sessions or self.path.exists():
                self._sessions.clear()
                self._save()


class LoginLimiter:
    """Slows down guessing: 10 wrong passwords from one device in 15 minutes block that device for
    15 minutes; 30 from all devices together block every sign-in from the network for 15 minutes."""

    WINDOW_S = 15 * 60
    BLOCK_S = 15 * 60
    PER_DEVICE = 10
    TOTAL = 30

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._blocked: dict[str, float] = {}  # ip ("" = everyone) → until
        self._lock = threading.Lock()

    def _prune(self, times: deque[float], now: float) -> None:
        while times and now - times[0] >= self.WINDOW_S:
            times.popleft()

    def _forget_old(self, now: float) -> None:
        """Devices that stopped guessing long ago, so the bookkeeping can't grow without end."""
        for ip in [ip for ip, times in self._failures.items() if not times or now - times[-1] >= self.WINDOW_S]:
            del self._failures[ip]
        for ip in [ip for ip, until in self._blocked.items() if until <= now]:
            del self._blocked[ip]

    def blocked_for(self, ip: str) -> int:
        """Seconds until this device may try again; 0 = it may now."""
        with self._lock:
            now = self.clock()
            until = max(self._blocked.get(ip, 0), self._blocked.get("", 0))
            return max(0, int(until - now + 0.999))

    def failed(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self._forget_old(now)
            times = self._failures.setdefault(ip, deque())
            times.append(now)
            self._all.append(now)
            self._prune(times, now)
            self._prune(self._all, now)
            if len(times) >= self.PER_DEVICE:
                self._blocked[ip] = now + self.BLOCK_S
                times.clear()
                log.warning("Admin sign-in from %s blocked for 15 minutes after too many wrong passwords", ip)
            if len(self._all) >= self.TOTAL:
                self._blocked[""] = now + self.BLOCK_S
                self._all.clear()
                log.warning("Admin sign-in from the network blocked for 15 minutes after too many wrong passwords")

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
