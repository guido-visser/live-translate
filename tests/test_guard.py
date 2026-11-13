import json
import time

from livetranslate import guard


def test_only_crash_codes_bring_the_app_back():
    assert guard.is_crash(0xC0000005)  # access violation (13 Nov 2026, GPU driver)
    assert guard.is_crash(-1073741819)  # the same, signed
    assert guard.is_crash(0xC0000409)  # fail-fast
    assert guard.is_crash(3)  # abort / Python fatal error
    assert not guard.is_crash(0)  # Exit in the tray, an update, the app restarting itself
    assert not guard.is_crash(1)  # Task Manager
    assert not guard.is_crash(0xC000013A)  # console closed, logoff


def test_crash_restarts_are_limited_per_half_hour():
    now = time.time()
    assert guard.allowed([], now)
    assert guard.allowed([now - 60, now - 30], now)
    assert not guard.allowed([now - 90, now - 60, now - 30], now)
    assert guard.allowed([now - guard.WINDOW_S - 1] * 5, now)


def test_restart_goes_on_translating_and_stops_after_too_many(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(guard.subprocess, "Popen", lambda command, **kw: started.append(command))
    (tmp_path / guard.TRANSLATING_FILE).touch()
    for _ in range(guard.MAX_CRASH_RESTARTS):
        assert guard.restart(tmp_path, 1234, 0xC0000005, ["--autostart"])
    assert not guard.restart(tmp_path, 1234, 0xC0000005, ["--autostart"])
    assert len(started) == guard.MAX_CRASH_RESTARTS
    command = started[0]
    assert command[1:3] == ["-m", "livetranslate"]
    assert "--autostart" in command and "--resume" in command
    assert command[command.index("--after") + 1] == "1234"
    assert len(json.loads((tmp_path / guard.HISTORY_FILE).read_text("utf-8"))) == guard.MAX_CRASH_RESTARTS
    assert "not starting it again" in (tmp_path / "livetranslate.log").read_text("utf-8")


def test_restart_without_translating_does_not_resume(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(guard.subprocess, "Popen", lambda command, **kw: started.append(command))
    guard.restart(tmp_path, 1, 0xC0000005, [])
    assert "--resume" not in started[0]


def test_guard_arguments(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(guard.sys, "platform", "win32")
    monkeypatch.setattr(guard, "_wait_for_exit", lambda pid: seen.setdefault("pid", pid) and 0xC0000005)
    monkeypatch.setattr(guard, "_shutting_down", lambda: False)
    monkeypatch.setattr(guard, "restart", lambda data, pid, code, args: seen.update(data=data, args=args))
    guard.main(["--pid", "42", "--data", str(tmp_path), "--", "--autostart", "--port", "8001"])
    assert seen == {"pid": 42, "data": tmp_path, "args": ["--autostart", "--port", "8001"]}
