import os
import re
import subprocess
import sys
from pathlib import Path

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


def test_installs_from_before_the_wizard_skip_it(tmp_path):
    from livetranslate.config import SettingsStore

    assert SettingsStore(tmp_path / "new.json").settings.setup_done is False
    old = tmp_path / "old.json"
    old.write_text('{"source_language": "nl"}')
    assert SettingsStore(old).settings.setup_done is True
    store = SettingsStore(tmp_path / "saved.json")
    store.save(store.settings)
    assert SettingsStore(store.path).settings.setup_done is False  # saved halfway: the wizard isn't done


def wizard_client(tmp_path, **settings):
    from fastapi.testclient import TestClient

    from livetranslate.config import SettingsStore
    from livetranslate.server.app import create_app

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(**settings))
    return store, TestClient(create_app(store, tmp_path, manage_translator=False), follow_redirects=False)


def test_operator_page_leads_to_the_wizard_until_it_is_finished(tmp_path):
    store, client = wizard_client(tmp_path)
    with client:
        assert client.get("/admin").headers["location"] == "/admin/setup"
        assert client.get("/admin/setup").status_code == 200
        assert client.post("/api/admin/setup/finish").status_code == 200
        assert store.settings.setup_done is True
        assert client.get("/admin").status_code == 200
        client.app.state.services.preparation.steps = ["an earlier run"]
        assert client.post("/api/admin/setup/again").status_code == 200
        assert client.app.state.services.preparation.steps == []  # opens at the start, not on the old result
        assert client.get("/admin?pin=1").headers["location"] == "/admin/setup?pin=1"


def test_nothing_is_assumed_about_a_church():
    settings = Settings()
    assert settings.target_languages == [] and settings.quick_languages == []
    assert settings.church_name == "" and settings.church_short_name == ""
    root = Path(__file__).parent.parent
    pages = [*(root / "src/livetranslate/web").rglob("*.html"), *(root / "src/livetranslate/web").rglob("*.js")]
    for page in pages:
        text = page.read_text("utf-8")
        for word in ("Kandelaar", "Candlestick", "Evangeliegemeente", "Cyrillic", "Cyrillisch", "кирилл", "кирил", "Кирил"):
            assert word not in text, f"{word} in {page.name}"
        if page.suffix == ".html" and page.name != "setup.html":  # that one lists the interface languages
            assert not re.search("[А-Яа-яЇїІіЄє]{3,}", text), f"text in one set of languages in {page.name}"


def test_a_church_with_several_names_keeps_them_all():
    from livetranslate.config import GlossaryEntry
    from livetranslate.translate.glossary import Glossary, is_name

    # As the name fields fill it in, for "Kandelaar, De Kandelaar".
    row = GlossaryEntry(lang="nl", term="Kandelaar, De Kandelaar, de Kandelaar", translations={"en": "Kandelaar"})
    assert is_name(row)
    glossary = Glossary([row])
    # Speech recognition spells it right too. Once: over music Whisper copied "De Kandelaar, de Kandelaar".
    assert glossary.hotwords("nl") == "Kandelaar"
    for spoken in ("Kandelaar", "De Kandelaar", "de Kandelaar"):
        prepared, terms = glossary.prepare(f"Welkom in {spoken} vandaag", "nl", "en", inline=True)
        assert terms == [spoken] and prepared == "Welkom in [1] vandaag"  # the longest form wins, not "Kandelaar"


def test_every_language_can_be_chosen_not_only_the_loaded_ones(tmp_path):
    # LibreTranslate only reports what it has loaded, which used to be all the settings offered.
    _, client = wizard_client(tmp_path, setup_done=True)
    with client:
        state = client.get("/api/admin/state").json()
    assert state["settings"]["translation"]["engine"] == "libretranslate"
    codes = {l["code"] for l in state["source_languages"]}
    assert {"nl", "en", "ru", "uk", "de", "fr", "es"} <= codes
    assert {"de", "fr"} <= {l["code"] for l in state["target_languages"]}
    assert state["settings"]["source_language"] not in {l["code"] for l in state["target_languages"]}
    assert "de" in {l["code"] for l in state["quick_language_options"]}
    assert "am" in state["catalogs"]["nllb"] and "am" not in state["catalogs"]["libretranslate"]


def test_download_progress_follows_the_furthest_count_of_bytes():
    from livetranslate.firstrun import Step, counting_bar

    step = Step("speech")
    bar = counting_bar(step)
    received = bar(total=0, unit="B", desc="Downloading bytes")
    written = bar(total=0, unit="B", desc="Reconstructing")
    files = bar(total=4, desc="Fetching 4 files")

    received.update(500)
    assert step.status()["done"] == 500
    written.update(200)  # behind what came in, and the two aren't added up
    files.update(1)  # files, not bytes
    assert step.done == 500
    written.update(400)
    assert step.done == 600
    received.update(-500)  # starting a file over doesn't make the bar run back
    assert step.done == 600


def test_preparation_downloads_what_was_chosen_and_reports_failures(tmp_path, monkeypatch):
    import time

    from livetranslate import firstrun

    store, client = wizard_client(tmp_path, target_languages=["ru", "de"], tts_enabled=True)
    services = client.app.state.services
    fetched, translated = [], []

    def fake_download(model, cache_dir):
        fetched.append(model)

    async def fake_speech(self, step):
        fake_download(self.services.settings.asr.model, "")

    async def fake_start():
        translated.append(True)
        services.translator.libre.status = "error"
        services.translator.libre.detail = "no internet"

    monkeypatch.setattr(firstrun.Preparation, "_speech", fake_speech)
    monkeypatch.setattr(services.translator, "start", fake_start)
    monkeypatch.setattr(services.speech.bank, "download_size", lambda langs: 2000)

    def fake_files(lang, progress):
        fetched.append(lang)
        progress(1000)

    monkeypatch.setattr(services.speech.bank, "_files", fake_files)

    with client:
        assert client.get("/api/admin/setup/status").json()["started"] is False
        client.post("/api/admin/setup/prepare")
        for _ in range(100):
            status = client.get("/api/admin/setup/status").json()
            if status["finished"]:
                break
            time.sleep(0.05)
    assert status["finished"] and not status["ok"]
    by_key = {s["key"]: s for s in status["steps"]}
    assert by_key["speech"]["state"] == "done"
    assert by_key["translation"]["state"] == "error" and by_key["translation"]["error"] == "no internet"
    assert by_key["voices"]["state"] == "done"  # goes on after a failure
    assert by_key["voices"]["done"] == by_key["voices"]["total"] == 2000
    assert fetched == [store.settings.asr.model, "ru", "de"] and translated


def test_the_app_closes_while_a_phone_is_listening(tmp_path):
    """An audio stream never ends by itself, and an update's installer waits for this process."""
    import socket
    import threading
    import time

    import httpx

    from livetranslate.__main__ import SHUTDOWN_WAIT_S, make_server
    from livetranslate.config import SettingsStore
    from livetranslate.server.app import create_app

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(target_languages=["ru"], tts_enabled=True))
    app = create_app(store, tmp_path, manage_translator=False)
    speech = app.state.services.speech
    speech.bank.has_voice = lambda lang: True
    speech.bank._files = lambda lang, progress=None: None
    speech.bank.prepare = lambda lang: None

    listening = socket.create_server(("127.0.0.1", 0))
    port = listening.getsockname()[1]
    server = make_server(app, port)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    def phone():
        try:
            with httpx.stream("GET", f"http://127.0.0.1:{port}/audio/ru.mp3", timeout=None) as response:
                for _ in response.iter_bytes():
                    pass
        except httpx.HTTPError:
            pass  # the server went away, as it should

    threading.Thread(target=phone, daemon=True).start()
    for _ in range(100):
        if speech.listener_counts():
            break
        time.sleep(0.05)
    assert speech.listener_counts() == {"ru": 1}

    server.should_exit = True
    thread.join(SHUTDOWN_WAIT_S + 5)
    still_running = thread.is_alive()
    server.force_exit = True  # don't leave it behind when this fails
    assert not still_running


def test_libretranslate_fetches_models_only_for_new_languages(tmp_path):
    from livetranslate.translate.libretranslate import LibreTranslate

    libre = LibreTranslate(Settings(source_language="nl", target_languages=["ru"]), tmp_path)
    libre.env_dir.mkdir()
    assert libre._models_installed() == set()
    libre.languages = [{"code": c} for c in ("en", "nl", "ru")]
    libre._remember_models()
    assert not set(libre.wanted) - libre._models_installed()
    libre.configure(Settings(source_language="nl", target_languages=["ru", "de"]))
    assert set(libre.wanted) - libre._models_installed() == {"de"}
    libre.languages = [{"code": c} for c in ("en", "nl")]  # started with fewer: what it had isn't forgotten
    libre._remember_models()
    assert "ru" in libre._models_installed()


def test_data_goes_to_the_picked_folder_and_settings_stay_put(tmp_path, monkeypatch):
    from livetranslate import config

    standard, picked = tmp_path / "standard", tmp_path / "Kérk data" / "LiveTranslate"
    standard.mkdir()
    monkeypatch.setattr(config, "user_data_dir", lambda *a, **k: str(standard))
    monkeypatch.setattr(config, "user_config_dir", lambda *a, **k: str(standard))
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "HF_HOME"):
        monkeypatch.delenv(name, raising=False)

    # Not picked (every install from before this existed): as it always was, also for the downloads.
    assert config.data_dir() == standard
    config.keep_downloads_in_data_dir()
    assert "HF_HOME" not in os.environ and "XDG_DATA_HOME" not in os.environ

    # Written like the setup window does: UTF-16 with a byte order mark.
    (standard / config.LOCATION_FILE).write_text(str(picked), "utf-16")
    assert config.data_dir() == picked and picked.is_dir()
    assert config.config_path().parent == standard  # the note and the settings stay where they can be found
    config.keep_downloads_in_data_dir()
    assert os.environ["XDG_DATA_HOME"].startswith(str(picked))  # LibreTranslate's models
    assert os.environ["HF_HOME"].startswith(str(picked))

    (standard / config.LOCATION_FILE).write_bytes(b"\xff")  # damaged: the standard folder
    assert config.data_dir() == standard


def test_setup_window_script_keeps_its_cyrillic_and_ships():
    folder = Path(__file__).parent.parent / "packaging" / "windows"
    # Windows PowerShell 5.1 reads a file without this mark as ANSI.
    assert (folder / "LiveTranslateSetup.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
    assert "LiveTranslateSetup.ps1" in (folder / "LiveTranslate.iss").read_text("utf-8")


def test_all_interface_languages_have_the_same_texts():
    import re

    source = (Path(__file__).parent.parent / "src/livetranslate/web/admin/i18n.js").read_text("utf-8")
    blocks = re.split(r"^  (?:nl|en|ru|uk): \{$", source, flags=re.M)[1:]
    keys = [set(re.findall(r"(?:^|[\s,])(\w+): \"", block, flags=re.M)) for block in blocks]
    assert len(keys) == 4 and all(k == keys[1] for k in keys), [sorted(k ^ keys[1]) for k in keys]


def test_setup_starts_the_app_through_explorer():
    # Setup's RedirectionGuard must not reach uv: it would refuse its own Python folder link (os error 448).
    script = Path(__file__).parent.parent / "packaging" / "windows" / "LiveTranslate.iss"
    runs = script.read_text("utf-8").split("[Run]")[1].split("[Registry]")[0]
    starts = [line for line in runs.splitlines() if "LiveTranslate.vbs" in line and line.startswith("Filename")]
    assert len(starts) == 2 and all(line.startswith('Filename: "{win}\\explorer.exe"') for line in starts)


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


def test_old_default_window_size_gets_the_new_default(tmp_path):
    path = tmp_path / "window.json"
    window.save_state(path, "Live Translate", {"width": window.OLD_DEFAULT[0], "height": window.OLD_DEFAULT[1], "maximized": False})
    assert window.load_state(path, "Live Translate")["height"] == window.HEIGHT
    window.save_state(path, "Live Translate", {"width": 1000, "height": 700, "maximized": False})
    assert window.load_state(path, "Live Translate")["height"] == 700  # someone chose this


def test_moving_data_only_while_idle_and_from_the_installed_app(tmp_path, monkeypatch):
    from livetranslate.server import app as server

    _, client = wizard_client(tmp_path, setup_done=True)
    started, closed = [], []
    monkeypatch.setattr(server, "start_data_move", started.append)
    services = client.app.state.services
    services.updater.request_exit = lambda: closed.append(True)
    with client:
        monkeypatch.setattr(server, "install_dir", lambda: None)
        assert client.get("/api/admin/state").json()["can_move_data"] is False
        assert client.post("/api/admin/data/move").status_code == 400  # running from source

        monkeypatch.setattr(server, "install_dir", lambda: tmp_path)
        assert client.get("/api/admin/state").json()["can_move_data"] is True
        services.pipeline.state = "running"
        assert client.post("/api/admin/data/move").status_code == 409
        assert not started

        services.pipeline.state = "stopped"
        assert client.post("/api/admin/data/move").status_code == 200
        assert started == [tmp_path]


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
