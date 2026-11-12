"""The start on 12 Nov 2026: slow, clicked three times, and then every page failed unnoticed."""

import logging
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from livetranslate import health, splash, tray
from livetranslate.__main__ import make_server, restart_command
from livetranslate.config import Settings, SettingsStore
from livetranslate.server.app import _quiet_disconnects, create_app
from livetranslate.server.files import WebFiles


@pytest.fixture
def app(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(setup_done=True))
    return create_app(store, tmp_path, manage_translator=False)


def test_pages_and_scripts_come_from_memory(app):
    with TestClient(app) as client:
        for path in ("/", "/qr", "/admin", "/manifest.webmanifest", "/static/icon.png", "/static/client/app.js"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers["cache-control"] == "no-cache"
        assert client.get("/").headers["content-type"].startswith("text/html")
        assert client.get("/static/admin/admin.css").headers["content-type"].startswith("text/css")
        head = client.head("/static/icon.png")
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(client.get("/static/icon.png").content))
        for missing in ("/static/nope.js", "/static/../server/app.py", "/static/%2e%2e/server/app.py"):
            assert client.get(missing).status_code == 404, missing


def test_a_changed_file_is_read_again(tmp_path):
    (tmp_path / "page.html").write_text("one")
    files = WebFiles(tmp_path)
    assert files.get("page.html").body == b"one"
    time.sleep(0.01)
    (tmp_path / "page.html").write_text("two!")
    assert files.get("page.html").body == b"two!"
    (tmp_path / "later.html").write_text("added")
    assert files.get("later.html") is None  # only what was there at the start


def test_health_endpoint_uses_the_worker_threads(app, monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/health").json() == {"ok": True}

        async def broken(*args, **kwargs):
            raise RuntimeError("There is no current event loop in thread 'server'.")

        monkeypatch.setattr("anyio.to_thread.run_sync", broken)
        assert client.get("/api/health").status_code == 500


def test_a_failed_request_says_which_address(app, caplog):
    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client, caplog.at_level(logging.ERROR):
        assert client.get("/boom").status_code == 500
    assert "Request failed: GET /boom" in caplog.text


def test_check_against_the_real_server(app):
    listening = socket.create_server(("127.0.0.1", 0))
    port = listening.getsockname()[1]
    server = make_server(app, port)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.02)
        assert health.check(port) is None
    finally:
        server.should_exit = True
        thread.join(10)
    assert health.check(port, timeout=1) is not None  # nothing there any more


class Run:
    def __init__(self, results, started=True):
        self.results = list(results)
        self.started = started
        self.stopping = False
        self.ready, self.broken, self.reports = 0, [], []

    def check(self):
        if not self.results:
            self.stopping = True  # end the test
            return None
        return self.results.pop(0)

    def watch(self):
        dog = health.Watchdog(
            is_started=lambda: self.started,
            is_stopping=lambda: self.stopping,
            on_ready=lambda: setattr(self, "ready", self.ready + 1),
            on_broken=self.broken.append,
            check=self.check,
            report=self.reports.append,
            interval_s=0,
            first_wait_s=0,
        )
        dog.run()
        return self


def test_ready_once_and_one_bad_minute_is_no_reason_to_restart():
    run = Run([None, "slow", None, "slow", "slow", None]).watch()
    assert run.ready == 1 and run.broken == []
    assert run.reports[:6] == [None, "slow", None, "slow", "slow", None]  # the tray shows each one


def test_pages_that_keep_failing_start_the_app_again():
    run = Run([None, *["/qr gave 500"] * health.FAILURES_BEFORE_RESTART, None]).watch()
    assert run.ready == 1 and run.broken == ["/qr gave 500"]


def test_a_start_that_never_works_is_not_reported_ready():
    run = Run(["/qr gave 500"] * health.FIRST_TRIES).watch()
    assert run.ready == 0 and run.broken == ["/qr gave 500"]
    run = Run(["slow"] * (health.FIRST_TRIES - 1) + [None]).watch()  # a slow start that works in the end
    assert run.ready == 1 and run.broken == []


def test_closing_app_is_not_broken():
    run = Run([])
    run.started, run.stopping = False, True
    run.watch()
    assert run.ready == 0 and run.broken == []


def test_restart_keeps_the_arguments_and_counts():
    first = restart_command(["--autostart", "--port", "8000"], 111, resume=True)
    assert first[:3] == [sys.executable, "-m", "livetranslate"]
    assert first[3:] == ["--autostart", "--port", "8000", "--after", "111", "--restarts", "1", "--resume"]
    second = restart_command(first[3:], 222, resume=False)
    assert second[3:] == ["--autostart", "--port", "8000", "--after", "222", "--restarts", "2"]


def test_a_phone_leaving_is_not_an_error():
    seen = []

    class Loop:
        def default_exception_handler(self, context):
            seen.append(context)

    _quiet_disconnects(Loop(), {"message": "x", "exception": ConnectionResetError(10054, "reset")})
    assert seen == []
    _quiet_disconnects(Loop(), {"message": "y", "exception": ValueError()})
    assert len(seen) == 1


def test_splash_and_problem_state_in_every_language():
    assert set(splash.TEXTS) == set(tray.TEXTS)
    assert tray.make_icon("problem").getpixel((8, 32)) != tray.make_icon("running").getpixel((8, 32))
