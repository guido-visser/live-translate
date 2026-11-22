"""Hardening for use on a local network; see also test_web_server.py and test_auth.py."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from livetranslate.config import AsrSettings, Settings, SettingsStore, is_network_path
from livetranslate.server import app as app_module
from livetranslate.server.app import create_app
from livetranslate.training import Trainer

PASSWORD = "correct horse"


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr("livetranslate.firewall.status", lambda port, fresh=False: "ok")
    store = SettingsStore(tmp_path / "settings.json")
    settings = Settings(source_language="nl", target_languages=["ru"], setup_done=True, tts_enabled=False)
    settings.obs.password = "obs-secret"
    store.save(settings)
    app = create_app(store, tmp_path, manage_translator=False)
    services = app.state.services
    services.login_delay_s = 0
    services.own_names = lambda: {"192.168.1.10"}
    with TestClient(app) as client:
        client.services = services
        yield client


def remote(local, ip="192.168.1.50", host="192.168.1.10:8000") -> TestClient:
    return TestClient(local.app, client=(ip, 50000), base_url=f"http://{host}")


def allow_remote(local) -> None:
    assert local.post("/api/admin/webserver/password", json={"password": PASSWORD}).status_code == 200
    assert local.post("/api/admin/webserver/lan", json={"on": True}).status_code == 200


def signed_in(local, ip="192.168.1.50") -> TestClient:
    tablet = remote(local, ip)
    assert tablet.post("/api/admin/login", json={"password": PASSWORD}).status_code == 200
    return tablet


def test_pages_carry_security_headers(local):
    page = remote(local).get("/")
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and "script-src 'self';" in policy
    assert "unsafe-eval" not in policy and "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    # The app window on this PC needs eval for its bridge to Python; nobody else gets it.
    assert "'unsafe-eval'" in local.get("/admin").headers["content-security-policy"]
    assert local.get("/api/admin/state").headers["cache-control"] == "no-store"


def test_oversized_requests_are_refused(local):
    allow_remote(local)
    big = "x" * (app_module.MAX_BODY + 1)
    assert remote(local).post("/api/admin/login", content=big, headers={"content-type": "application/json"}).status_code == 413
    # Without saying the size up front, too.
    def chunks():
        for _ in range(20):
            yield b"x" * (app_module.MAX_BODY // 10)

    assert local.post("/api/admin/settings", content=chunks()).status_code == 413


def test_a_session_only_works_from_where_it_signed_in(local):
    allow_remote(local)
    tablet = signed_in(local, "192.168.1.50")
    thief = remote(local, "192.168.1.66")
    thief.cookies = tablet.cookies
    assert thief.get("/api/admin/state").status_code == 401
    assert tablet.get("/api/admin/state").status_code == 200


def test_the_admin_page_answers_only_to_this_pcs_own_names(local):
    allow_remote(local)
    rebound = remote(local, host="evil.example:8000")
    assert rebound.get("/admin/login").status_code == 403
    assert rebound.post("/api/admin/login", json={"password": PASSWORD}).status_code == 403
    tablet = signed_in(local)
    tablet.headers["host"] = "evil.example:8000"
    assert tablet.get("/api/admin/state").status_code == 403


def test_listening_over_the_internet_keeps_the_admin_page_on_this_pc(local):
    allow_remote(local)
    tablet = signed_in(local)

    async def no_tunnel(settings):
        pass

    local.services.tunnel.apply = no_tunnel  # no real ssh in a test
    settings = local.get("/api/admin/state").json()["settings"]
    settings["remote"] ={**settings["remote"], "enabled": True, "ssh_host": "server.example", "ssh_user": "lt"}
    assert local.post("/api/admin/settings", json=settings).status_code == 200
    assert tablet.get("/api/admin/state").status_code == 403
    assert remote(local).post("/api/admin/login", json={"password": PASSWORD}).status_code == 403
    web = local.get("/api/admin/webserver").json()
    assert web["tunnel_on"] is True and web["admin_urls"] == []
    assert local.get("/api/admin/state").status_code == 200  # this PC keeps working


def test_other_devices_never_see_or_change_this_pcs_secrets_and_paths(local):
    allow_remote(local)
    tablet = signed_in(local)
    settings = tablet.get("/api/admin/state").json()["settings"]
    assert settings["obs"]["password"] == "" and "obs-secret" not in tablet.get("/api/admin/state").text
    changed = {
        **settings,
        "audio": {**settings["audio"], "file_path": "C:\\Users\\someone\\secret.wav"},
        "libretranslate_url": "http://collector.example",
        "obs": {**settings["obs"], "password": ""},
    }
    assert tablet.post("/api/admin/settings", json=changed).status_code == 200
    s = local.services.settings
    assert s.obs.password == "obs-secret" and s.audio.file_path == "" and s.libretranslate_url == ""
    assert local.get("/api/admin/state").json()["settings"]["obs"]["password"] == "obs-secret"


def test_network_share_paths_are_never_opened(local, tmp_path):
    for path in ("\\\\server\\share\\a.wav", "//server/share/a.wav", "\\\\?\\UNC\\server\\a.wav", ' "\\\\server\\a"'):
        assert is_network_path(path), path
    assert not is_network_path("C:\\audio\\a.wav") and not is_network_path("a.wav")
    settings = local.get("/api/admin/state").json()["settings"]
    settings["audio"] = {**settings["audio"], "file_path": "\\\\attacker\\share\\a.wav"}
    assert local.post("/api/admin/settings", json=settings).status_code == 400
    trainer = Trainer(tmp_path, lambda: Settings(), lambda: True, lambda m: None)
    assert trainer.add("\\\\attacker\\share\\a.wav")["rejected"] == ["\\\\attacker\\share\\a.wav"]


def test_other_devices_cannot_add_files_for_training(local, tmp_path):
    allow_remote(local)
    sample = tmp_path / "sermon.wav"
    sample.write_bytes(b"RIFF")
    result = signed_in(local).post("/api/admin/training/add", json={"text": str(sample)}).json()
    assert result["added"] == 0 and result["rejected"] == [str(sample)]
    assert local.post("/api/admin/training/add", json={"text": str(sample)}).json()["added"] == 1


def test_removing_a_training_source_only_touches_listed_files(tmp_path):
    trainer = Trainer(tmp_path, lambda: Settings(), lambda: True, lambda m: None)
    outside = tmp_path / "keep.npz"
    outside.write_bytes(b"x")
    trainer.remove("..\\keep")
    trainer.remove("../keep")
    assert outside.exists()


def test_service_log_names_cannot_leave_the_logs_folder(local):
    logs = local.services.pipeline.logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    (logs.parent / "events.jsonl").write_text("{}\n", "utf-8")  # what ".." would find
    for name in ("..", ".", "..\\x", "a..\\..\\b"):
        assert local.get(f"/api/admin/logs/{name}/analysis").status_code == 404, name


def test_listeners_are_limited_per_device(local, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_LISTENERS_PER_DEVICE", 2)
    phone = remote(local)
    with phone.websocket_connect("/ws/listen") as first, phone.websocket_connect("/ws/listen") as second:
        first.receive_json(), second.receive_json()
        with pytest.raises(WebSocketDisconnect) as refused:
            with phone.websocket_connect("/ws/listen") as third:
                third.receive_json()
        assert refused.value.code == 1013
        other = remote(local, "192.168.1.51")
        with other.websocket_connect("/ws/listen") as fine:
            assert fine.receive_json()["type"] == "init"
    assert not local.services.listener_devices  # all given back


def test_the_qr_page_and_addresses_are_for_admins_only(local):
    phone = remote(local)
    assert phone.get("/api/qr/addresses").status_code == 403
    assert phone.get("/api/qr.svg").status_code == 403
    assert phone.get("/qr", follow_redirects=False).status_code == 403
    allow_remote(local)
    assert phone.get("/qr", follow_redirects=False).headers["location"] == "/admin/login?next=/qr"
    assert local.get("/api/qr/addresses").status_code == 200
    assert signed_in(local).get("/api/qr.svg").status_code == 200


def test_only_offered_speech_models_are_loaded():
    assert AsrSettings(model="medium").model == "medium"
    assert AsrSettings(model="C:\\models\\anything").model == "large-v3-turbo"
    assert AsrSettings(model="someone/whisper-on-hugging-face").model == "large-v3-turbo"
    with pytest.raises(ValueError):
        AsrSettings(beam_size=1000)


def test_test_names_never_count_as_this_pc_in_the_app():
    import importlib

    fresh = importlib.reload(app_module)
    try:
        assert "testserver" not in fresh.LOCAL_NAMES and "testclient" not in fresh.LOCAL_HOSTS
    finally:
        importlib.reload(app_module)
