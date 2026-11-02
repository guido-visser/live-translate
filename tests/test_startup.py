import subprocess
import sys

import pytest

from livetranslate import autostart, window
from livetranslate.config import Settings


def test_new_options_leave_existing_installs_alone():
    s = Settings()
    assert s.start_with_windows is False
    assert s.auto_start_translation is False
    assert s.app_window is True
    assert s.start_window == "tray"
    assert s.service_log.enabled is False and s.service_log.audio is False  # nothing stored unless chosen


def test_church_profile_holds_the_church_and_nothing_private(tmp_path):
    from fastapi.testclient import TestClient

    from livetranslate.config import PROFILE_FIELDS, SettingsStore
    from livetranslate.server.app import create_app

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(church_name="Evangeliegemeente De Kandelaar", admin_pin="1234"))
    with TestClient(create_app(store, tmp_path, manage_translator=False)) as client:
        response = client.get("/api/admin/profile")
        assert client.get("/api/admin/state").json()["profile_fields"] == list(PROFILE_FIELDS)
    assert "attachment" in response.headers["content-disposition"]
    profile = response.json()
    assert profile["church_name"] == "Evangeliegemeente De Kandelaar"
    assert set(profile) == set(PROFILE_FIELDS)
    assert "1234" not in response.text  # no PIN, server or tunnel details in a file meant for sharing


def test_autostart_command_uses_the_installed_launcher(monkeypatch, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "LiveTranslate.vbs").write_text("")
    monkeypatch.setenv("LIVETRANSLATE_UV", str(tmp_path / "bin" / "uv.exe"))
    command = autostart.command()
    assert f'"{tmp_path / "LiveTranslate.vbs"}"' in command
    assert command.endswith("--autostart")  # the app then follows the start_window setting


def test_autostart_command_from_source(monkeypatch):
    monkeypatch.delenv("LIVETRANSLATE_UV", raising=False)
    assert autostart.command().endswith("-m livetranslate --autostart")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_autostart_writes_and_removes_the_run_value():
    import winreg

    name = "LiveTranslateTest"

    def stored():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]

    try:
        assert autostart.apply(True, name)
        assert stored() == autostart.command()
    finally:
        assert autostart.apply(False, name)
    with pytest.raises(FileNotFoundError):
        stored()
    assert autostart.apply(False, name)  # removing twice is fine


def test_window_state_defaults_until_it_was_saved(tmp_path):
    path = tmp_path / "window.json"
    default = {"width": window.WIDTH, "height": window.HEIGHT, "maximized": False}
    assert window.load_state(path, "Live Translate") == default
    saved = {"width": 900, "height": 700, "maximized": True}
    window.save_state(path, "Live Translate", saved)
    window.save_state(path, "QR", {"width": 500, "height": 600, "maximized": False})
    assert window.load_state(path, "Live Translate") == saved  # each window keeps its own
    assert window.load_state(path, "Other") == default


@pytest.mark.parametrize("content", ["not json", "[]", '{"Live Translate": {"width": "x"}}', '{"Live Translate": 3}'])
def test_window_state_survives_a_damaged_file(tmp_path, content):
    path = tmp_path / "window.json"
    path.write_text(content)
    assert window.load_state(path, "Live Translate")["width"] == window.WIDTH
    window.save_state(path, "Live Translate", {"width": 900, "height": 700, "maximized": False})
    assert window.load_state(path, "Live Translate")["width"] == 900


def test_window_opens_centered_and_on_screen():
    assert window.centered(900, 700, (0, 0, 1920, 1040)) == (510, 170, 900, 700)
    assert window.centered(900, 700, (100, 40, 1920, 1040)) == (610, 210, 900, 700)  # taskbar on the left or top
    assert window.centered(1900, 1000, (0, 0, 1280, 680)) == (0, 0, 1280, 680)  # now on a smaller screen
    assert window.centered(100, 100, (0, 0, 1920, 1040))[2:] == window.MIN_SIZE


def test_open_page_falls_back_to_the_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    window.open_page("http://x/admin", use_window=False)
    assert opened == ["http://x/admin"]


@pytest.mark.skipif(sys.platform != "win32", reason="the window is Windows-only")
@pytest.mark.parametrize(
    "exit_code, browser",
    [(0, False), (1, True), (None, False)],  # came to the front / can't start / window is up
)
def test_open_page_uses_a_window_unless_it_fails(monkeypatch, exit_code, browser):
    opened = []

    class FakeProcess:
        returncode = exit_code

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            if exit_code is None:
                raise subprocess.TimeoutExpired("window", timeout)
            return exit_code

    monkeypatch.setattr(window.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    window.open_page("http://x/admin")
    assert bool(opened) is browser
