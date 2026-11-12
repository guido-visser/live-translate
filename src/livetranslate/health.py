"""The app checks itself the way a phone or the operator window would use it.

On 12 Nov 2026 the app came up after a very slow start (Windows updates) with every page
failing while /api/public, the only thing anything checked, kept answering. Nobody could
tell until they looked. Now the app loads a real page once the server is up and every
minute after that; when that keeps failing it starts itself again.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

FIRST_TRIES = 6  # right after the start the PC may still be very busy
FIRST_WAIT_S = 5
INTERVAL_S = 60
FAILURES_BEFORE_RESTART = 3  # in a row, a minute apart: not one slow moment
TIMEOUT_S = 20


def check(port: int, timeout: float = TIMEOUT_S) -> str | None:
    """None when the pages work; otherwise what went wrong."""
    import httpx

    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=timeout) as client:
            for path in ("/api/health", "/qr", "/static/icon.png"):
                r = client.get(base + path)
                if r.status_code != 200:
                    return f"{path} gave {r.status_code}"
                if path == "/api/health" and not r.json().get("ok"):
                    return f"{path} says the pages are missing"
    except (httpx.HTTPError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class Watchdog:
    """Waits for the server, reports when it works (once), and keeps checking.

    is_started / is_stopping come from the uvicorn server. on_ready runs once, after the first
    good check; on_broken runs once, when the pages keep failing, and ends the watch."""

    def __init__(
        self,
        is_started: Callable[[], bool],
        is_stopping: Callable[[], bool],
        on_ready: Callable[[], None],
        on_broken: Callable[[str], None],
        check: Callable[[], str | None],
        report: Callable[[str | None], None] = lambda problem: None,
        interval_s: float = INTERVAL_S,
        first_wait_s: float = FIRST_WAIT_S,
    ):
        self.is_started = is_started
        self.is_stopping = is_stopping
        self.on_ready = on_ready
        self.on_broken = on_broken
        self._check = check
        self.report = report  # each result, for the tray: None when the pages work
        self.interval_s = interval_s
        self.first_wait_s = first_wait_s
        self._stop = threading.Event()

    def check(self) -> str | None:
        problem = self._check()
        try:
            self.report(problem)
        except Exception:
            log.debug("Reporting the check failed", exc_info=True)
        return problem

    def stop(self) -> None:
        self._stop.set()

    def _sleep(self, seconds: float) -> bool:
        """False when it's time to stop."""
        return not self._stop.wait(seconds) and not self.is_stopping()

    def run(self) -> None:
        # No time limit: on 12 Nov the start took three minutes, and a fixed 20 s gave up too soon.
        while not self.is_started():
            if not self._sleep(0.2):
                return
        problem = None
        for attempt in range(FIRST_TRIES):
            problem = self.check()
            if problem is None:
                break
            log.warning("The app's pages don't work yet (%d of %d): %s", attempt + 1, FIRST_TRIES, problem)
            if not self._sleep(self.first_wait_s):
                return
        if problem is not None:
            self._broken(problem)
            return
        log.info("The pages work")
        try:
            self.on_ready()
        except Exception:
            log.exception("Opening the app after the start failed")
        failures = 0
        while self._sleep(self.interval_s):
            problem = self.check()
            if problem is None:
                if failures:
                    log.info("The pages work again")
                failures = 0
                continue
            failures += 1
            log.warning("The app's pages don't work (%d of %d): %s", failures, FAILURES_BEFORE_RESTART, problem)
            if failures >= FAILURES_BEFORE_RESTART:
                self._broken(problem)
                return

    def _broken(self, problem: str) -> None:
        log.error("THE APP'S PAGES DON'T WORK (%s): phones and the operator window can't load them", problem)
        if not self.is_stopping():
            self.on_broken(problem)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="watchdog", daemon=True)
        thread.start()
        return thread
