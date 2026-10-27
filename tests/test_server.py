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
    libre, nllb = services.translator.libre, services.translator.nllb
    libre.languages = LANGUAGES
    libre.status = "ready"

    async def fake_libre(text, source, target):
        return f"[{target}] {text}"

    async def fake_nllb(text, source, target):
        return f"[nllb {target}] {text}"

    async def nothing():
        pass

    libre.translate = fake_libre
    libre.restart = libre.stop = nothing
    nllb.translate = fake_nllb

    async def fake_nllb_start():
        nllb.status = "ready"

    nllb.start = fake_nllb_start
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
        client.portal.call(pipeline._sentences.put_nowait, ("Goedemorgen allemaal.", time.time(), "nl"))
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


def test_switching_to_nllb_retranslates_history(client):
    pipeline = client.services.pipeline
    with client.websocket_connect("/ws/listen") as ws:
        ws.receive_json()
        ws.send_json({"type": "lang", "lang": "uk"})
        receive_until(ws, "history")
        client.portal.call(pipeline._sentences.put_nowait, ("Laten we bidden.", time.time(), "nl"))
        assert receive_until(ws, "segment")["text"] == "[uk] Laten we bidden."

        settings = client.get("/api/admin/state").json()["settings"]
        settings["translation"]["engine"] = "nllb"
        client.post("/api/admin/settings", json=settings)
        languages = receive_until(ws, "languages")
        assert "uk" in [l["code"] for l in languages["languages"]]

        state = client.get("/api/admin/state").json()
        assert state["translator"] == {"engine": "nllb", "status": "ready", "detail": "", "needs_restart": False}
        client.portal.call(pipeline._sentences.put_nowait, ("Amen.", time.time(), "nl"))
        assert receive_until(ws, "segment")["text"] == "[nllb uk] Amen."

    with client.websocket_connect("/ws/listen") as ws:
        ws.receive_json()
        ws.send_json({"type": "lang", "lang": "uk"})
        texts = [s["text"] for s in receive_until(ws, "history")["segments"]]
        assert texts == ["[nllb uk] Laten we bidden.", "[nllb uk] Amen."]


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


def test_testimony_in_russian_goes_to_phones_and_vmix(client):
    from tests.test_vmix import FakeVmix

    fake = FakeVmix()
    client.services.vmix._transport = fake.transport
    settings = client.get("/api/admin/state").json()["settings"]
    settings["vmix"].update(enabled=True, input="bbb-222", field="Headline.Text", overlay=1)
    client.post("/api/admin/settings", json=settings)

    state = client.get("/api/admin/state").json()
    assert [l["code"] for l in state["speaker_languages"]] == ["nl", "ru", "uk"]
    assert {l["code"] for l in state["quick_language_options"]} >= {"ru", "uk", "en"}
    assert client.get("/api/admin/vmix/inputs").json()["inputs"][0]["key"] == "bbb-222"

    pipeline = client.services.pipeline
    sockets = {}
    with client.websocket_connect("/ws/listen") as ru, client.websocket_connect("/ws/listen") as uk, \
            client.websocket_connect("/ws/listen") as nl:
        for lang, ws in (("ru", ru), ("uk", uk), ("nl", nl)):
            ws.receive_json()
            ws.send_json({"type": "lang", "lang": lang})
            receive_until(ws, "history")
            sockets[lang] = ws

        assert client.post("/api/admin/speaker", json={"lang": "ru"}).json()["speaker_language"] == "ru"
        assert fake.calls[:2] == [
            {"Function": "SetText", "Input": "bbb-222", "SelectedName": "Headline.Text", "Value": ""},
            {"Function": "OverlayInput1In", "Input": "bbb-222"},
        ]

        client.portal.call(pipeline._sentences.put_nowait, ("Я из Харькова.", time.time(), "ru"))
        assert receive_until(ru, "segment")["text"] == "Я из Харькова."
        assert receive_until(uk, "segment")["text"] == "[uk] Я из Харькова."
        assert receive_until(nl, "segment")["text"] == "[nl] Я из Харькова."
        assert fake.calls[2]["Value"] == "[nl] Я из Харькова."

        client.post("/api/admin/speaker", json={"lang": "nl"})
        assert fake.calls[-1] == {"Function": "OverlayInput1Out"}

        # Dutch speech goes to vMix as it is (vMix shows the main language).
        client.portal.call(pipeline._sentences.put_nowait, ("Dank je wel.", time.time(), "nl"))
        assert receive_until(nl, "segment")["text"] == "Dank je wel."
        assert fake.calls[-1]["Value"].endswith("Dank je wel.")

    assert client.post("/api/admin/speaker", json={"lang": "xx"}).status_code == 400


def test_quick_languages_setting_drives_speaker_buttons(client):
    settings = client.get("/api/admin/state").json()["settings"]
    settings["quick_languages"] = ["uk", "en"]
    state = client.post("/api/admin/settings", json=settings).json()
    assert [l["code"] for l in state["speaker_languages"]] == ["nl", "uk", "en"]
    assert client.post("/api/admin/speaker", json={"lang": "ru"}).status_code == 400
    assert client.get("/admin/settings").status_code == 200


def test_vmix_can_show_another_language(client):
    from tests.test_vmix import FakeVmix

    fake = FakeVmix()
    client.services.vmix._transport = fake.transport
    settings = client.get("/api/admin/state").json()["settings"]
    settings["vmix"].update(enabled=True, input="bbb-222", field="Headline.Text", language="en", overlay=7)
    client.post("/api/admin/settings", json=settings)
    pipeline = client.services.pipeline

    with client.websocket_connect("/ws/listen") as ws:  # keep the worker busy-free; no listeners needed
        ws.receive_json()
        client.portal.call(pipeline._sentences.put_nowait, ("Goedemorgen.", time.time(), "nl"))
        client.portal.call(pipeline._sentences.put_nowait, ("I am from Kyiv.", time.time(), "en"))
        for _ in range(50):
            if len(fake.calls) >= 2:
                break
            time.sleep(0.05)
    assert [c["Value"] for c in fake.calls] == ["[en] Goedemorgen.", "[en] Goedemorgen.\nI am from Kyiv."]

    # Switching the spoken language to the vMix language hides the overlay (no translation needed).
    settings["quick_languages"] = ["en"]
    client.post("/api/admin/settings", json=settings)
    client.post("/api/admin/speaker", json={"lang": "en"})
    assert fake.calls[-2:] == [
        {"Function": "SetText", "Input": "bbb-222", "SelectedName": "Headline.Text", "Value": ""},
        {"Function": "OverlayInput7Out"},
    ]


def test_gain_applies_live_without_restart(client):
    pipeline = client.services.pipeline
    restarts = []

    async def fake_restart():
        restarts.append(True)

    pipeline.restart = fake_restart
    pipeline._source = type("FakeSource", (), {"gain": 1.0, "level_db": -120.0})()

    settings = client.get("/api/admin/state").json()["settings"]
    settings["audio"]["gain"] = 2.5
    client.post("/api/admin/settings", json=settings)
    assert pipeline._source.gain == 2.5 and restarts == []

    settings["audio"]["channel"] = 2
    client.post("/api/admin/settings", json=settings)
    assert restarts == [True]
    pipeline._source = None
