import time

import pytest
from fastapi.testclient import TestClient

from livetranslate.config import Settings, SettingsStore
from livetranslate.server.app import create_app

LANGUAGES = [
    {"code": "nl", "name": "Dutch", "targets": ["en", "ru", "uk", "nl"]},
    {"code": "en", "name": "English", "targets": ["nl", "ru", "uk", "en"]},
    {"code": "ru", "name": "Russian", "targets": ["en", "nl", "uk", "ru"]},
    {"code": "uk", "name": "Ukrainian", "targets": ["en", "nl", "ru", "uk"]},
]


@pytest.fixture
def client(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(target_languages=["ru", "uk"], tts_enabled=False))
    app = create_app(store, tmp_path, manage_translator=False)
    services = app.state.services
    services.translator.languages = LANGUAGES
    services.translator.status = "ready"

    async def fake_translate(text, source, target):
        return f"[{target}] {text}"

    services.translator.translate = fake_translate
    with TestClient(app) as c:
        c.services = services
        yield c


def receive_until(ws, msg_type):
    for _ in range(20):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no {msg_type} message")


def test_listener_gets_languages_and_translated_segments(client):
    with client.websocket_connect("/ws/listen") as ws:
        init = ws.receive_json()
        assert init["type"] == "init"
        assert [l["code"] for l in init["languages"]] == ["nl", "ru", "uk"]
        assert next(l for l in init["languages"] if l["code"] == "uk")["name"] == "Українська"

        ws.send_json({"type": "lang", "lang": "uk"})
        assert receive_until(ws, "history")["segments"] == []

        pipeline = client.services.pipeline
        client.portal.call(pipeline._sentences.put_nowait, ("Goedemorgen allemaal.", time.time()))
        seg = receive_until(ws, "segment")
        assert seg["text"] == "[uk] Goedemorgen allemaal."
        assert seg["lang"] == "uk"

    # A new listener gets the history translated into their language.
    with client.websocket_connect("/ws/listen") as ws:
        ws.receive_json()
        ws.send_json({"type": "lang", "lang": "ru"})
        history = receive_until(ws, "history")
        assert [s["text"] for s in history["segments"]] == ["[ru] Goedemorgen allemaal."]


def test_unknown_language_is_rejected(client):
    with client.websocket_connect("/ws/listen") as ws:
        ws.receive_json()
        ws.send_json({"type": "lang", "lang": "xx"})
        assert receive_until(ws, "error")["error"] == "unknown-language"


def test_admin_state_and_settings_roundtrip(client):
    state = client.get("/api/admin/state").json()
    assert state["settings"]["target_languages"] == ["ru", "uk"]
    assert {l["code"] for l in state["target_languages"]} >= {"ru", "uk", "en"}

    settings = state["settings"]
    settings["target_languages"] = ["ru", "uk", "en"]
    settings["wifi_name"] = "Kerk-Gasten"
    client.services.translator._loaded = ("en", "nl", "ru", "uk")  # avoid a real restart
    new_state = client.post("/api/admin/settings", json=settings).json()
    assert new_state["settings"]["wifi_name"] == "Kerk-Gasten"
    assert client.get("/api/public").json()["wifi"] == "Kerk-Gasten"


def test_pause_is_broadcast(client):
    with client.websocket_connect("/ws/listen") as ws:
        ws.receive_json()
        assert client.post("/api/admin/pause", json={"paused": True}).json() == {"paused": True}
        assert receive_until(ws, "status")["paused"] is True


def test_qr_code_and_pages(client):
    assert client.get("/api/qr.svg").text.startswith("<svg")
    assert "ws/listen" in client.get("/static/client/app.js").text
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.get("/audio/ru.mp3").status_code == 404  # TTS disabled in this fixture
