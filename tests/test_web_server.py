"""Settings → Web Server: the admin page from other devices on the network."""

import json
import socket
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from livetranslate.config import Settings, SettingsStore
from livetranslate.server.app import _safe_next, create_app
from livetranslate.server.auth import COOKIE, LoginLimiter, check_password

PASSWORD = "correct horse"
ADMIN_URL = "http://192.168.1.10:8000/admin"


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr("livetranslate.firewall.status", lambda port, fresh=False: "ok")
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(source_language="nl", target_languages=["ru"], setup_done=True, tts_enabled=False))
    app = create_app(store, tmp_path, manage_translator=False)
    services = app.state.services
    services.login_delay_s = 0
    services.admin_urls = lambda: [{"name": "Church", "detail": "", "url": ADMIN_URL}]
    services.own_names = lambda: {"192.168.1.10", "church-pc"}
    with TestClient(app) as client:
        client.services = services
        yield client


def remote(local, ip="192.168.1.50") -> TestClient:
    """A tablet on the network."""
    return TestClient(local.app, client=(ip, 50000), base_url="http://192.168.1.10:8000")


def allow_remote(local) -> None:
    assert local.post("/api/admin/webserver/password", json={"password": PASSWORD}).status_code == 200
    assert local.post("/api/admin/webserver/lan", json={"on": True}).status_code == 200


def signed_in(local, ip="192.168.1.50") -> TestClient:
    tablet = remote(local, ip)
    assert tablet.post("/api/admin/login", json={"password": PASSWORD}).status_code == 200
    return tablet


def test_other_devices_get_nothing_while_it_is_off(local):
    tablet = remote(local)
    assert tablet.get("/admin", follow_redirects=False).status_code == 403
    assert tablet.get("/admin/login").status_code == 403
    assert tablet.post("/api/admin/login", json={"password": "x"}).status_code == 403
    assert tablet.get("/api/admin/state").status_code == 403
    assert tablet.get("/").status_code == 200  # the listener page is for everyone
    # A password alone doesn't open it: the switch must be on too.
    assert local.post("/api/admin/webserver/password", json={"password": PASSWORD}).status_code == 200
    assert tablet.post("/api/admin/login", json={"password": PASSWORD}).status_code == 403


def test_the_switch_needs_a_password(local):
    assert local.post("/api/admin/webserver/lan", json={"on": True}).status_code == 400
    assert local.post("/api/admin/webserver/password", json={"password": "short"}).status_code == 400
    web = local.get("/api/admin/webserver").json()
    assert web["password_set"] is False and web["lan_admin"] is False


def test_signing_in_from_a_tablet(local):
    allow_remote(local)
    tablet = remote(local)
    page = tablet.get("/admin/settings", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/admin/login?next=/admin/settings"
    assert tablet.get("/admin/login").status_code == 200
    assert tablet.get("/api/admin/state").status_code == 401
    assert tablet.post("/api/admin/login", json={"password": "wrong password"}).status_code == 401
    response = tablet.post("/api/admin/login", json={"password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    state = tablet.get("/api/admin/state")
    assert state.status_code == 200 and state.json()["access"] == "remote"
    assert "password_hash" not in state.text and "scrypt" not in state.text
    assert tablet.get("/admin", follow_redirects=False).status_code == 200
    assert tablet.post("/api/admin/clear").status_code == 200  # operating works
    assert local.get("/api/admin/state").json()["access"] == "local"


def test_this_pc_never_needs_to_sign_in(local):
    allow_remote(local)
    assert local.post("/api/admin/login", json={}).json() == {"ok": True}
    assert local.get("/admin/login?next=/admin/settings", follow_redirects=False).headers["location"] == "/admin/settings"
    assert local.get("/admin").status_code == 200


def test_guessing_is_slowed_down_and_blocked(local):
    allow_remote(local)
    tablet = remote(local)
    for _ in range(LoginLimiter.PER_DEVICE - 1):
        assert tablet.post("/api/admin/login", json={"password": "guess"}).status_code == 401
    blocked = tablet.post("/api/admin/login", json={"password": "guess"})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    assert tablet.post("/api/admin/login", json={"password": PASSWORD}).status_code == 429
    assert signed_in(local, "192.168.1.51")  # another device still can


def test_pc_only_actions_are_refused_to_other_devices(local):
    allow_remote(local)
    tablet = signed_in(local)
    for path in (
        "/api/admin/update/install",
        "/api/admin/logs/open",
        "/api/admin/upload?name=a.wav",
        "/api/admin/tunnel/key",
        "/api/admin/tunnel/forget",
        "/api/admin/data/move",
        "/api/admin/webserver/password",
        "/api/admin/webserver/lan",
        "/api/admin/webserver/port",
        "/api/admin/webserver/firewall",
        "/api/admin/webserver/restart",
        "/api/admin/webserver/sessions/revoke-all",
    ):
        assert tablet.post(path, json={"password": "taken over!", "on": False, "port": 9000}).status_code == 403, path
    assert tablet.post("/api/admin/close", json={"action": "exit"}).status_code == 403
    assert tablet.get("/api/admin/webserver").status_code == 403
    assert tablet.delete("/api/admin/webserver/sessions/x").status_code == 403
    assert local.services.settings.web_server.lan_admin is True
    assert check_password(PASSWORD, local.services.settings.web_server.password_hash)


def test_saving_settings_never_changes_the_web_server_or_from_a_tablet_the_tunnel(local):
    allow_remote(local)
    tablet = signed_in(local)
    settings = tablet.get("/api/admin/state").json()["settings"]
    changed = {
        **settings,
        "wifi_name": "Church Wi-Fi",
        "web_server": {"lan_admin": False, "password_hash": "", "password_short": False},
        "remote": {**settings["remote"], "ssh_host": "attacker.example"},
    }
    assert tablet.post("/api/admin/settings", json=changed).status_code == 200
    s = local.services.settings
    assert s.wifi_name == "Church Wi-Fi"
    assert s.web_server.lan_admin is True and s.web_server.password_hash
    assert s.remote.ssh_host == ""
    # On this PC the tunnel can be set, but the web server still only through its own tab.
    assert local.post("/api/admin/settings", json=changed).status_code == 200
    assert local.services.settings.remote.ssh_host == "attacker.example"
    assert local.services.settings.web_server.lan_admin is True


def test_everyone_is_signed_out_by_a_new_password_or_switching_off(local):
    allow_remote(local)
    tablet = signed_in(local)
    assert local.post("/api/admin/webserver/password", json={"password": "another password"}).status_code == 200
    assert tablet.get("/api/admin/state").status_code == 401
    assert tablet.post("/api/admin/login", json={"password": "another password"}).status_code == 200
    assert local.post("/api/admin/webserver/lan", json={"on": False}).status_code == 200
    assert tablet.get("/api/admin/state").status_code == 403
    assert local.post("/api/admin/webserver/lan", json={"on": True}).status_code == 200
    assert tablet.get("/api/admin/state").status_code == 401  # switching on again doesn't bring sessions back


def test_signing_out_devices(local):
    allow_remote(local)
    tablet, laptop = signed_in(local, "192.168.1.50"), signed_in(local, "192.168.1.51")
    sessions = local.get("/api/admin/webserver").json()["sessions"]
    assert {s["ip"] for s in sessions} == {"192.168.1.50", "192.168.1.51"}
    tablet_id = next(s["id"] for s in sessions if s["ip"] == "192.168.1.50")
    left = local.delete(f"/api/admin/webserver/sessions/{tablet_id}").json()["sessions"]
    assert [s["ip"] for s in left] == ["192.168.1.51"]
    assert tablet.get("/api/admin/state").status_code == 401
    assert laptop.get("/api/admin/state").status_code == 200
    assert laptop.post("/api/admin/logout").status_code == 200
    assert laptop.get("/api/admin/state").status_code == 401
    assert local.get("/api/admin/webserver").json()["sessions"] == []
    signed_in(local)
    assert local.post("/api/admin/webserver/sessions/revoke-all").json()["sessions"] == []


def test_an_open_admin_page_is_disconnected_when_signed_out(local):
    allow_remote(local)
    tablet = signed_in(local)
    # A browser sends the cookie with the websocket too; the test client needs to be told.
    cookie = {"cookie": f"{COOKIE}={tablet.cookies[COOKIE]}", "host": "192.168.1.10:8000"}
    with tablet.websocket_connect("/ws/admin", headers=cookie) as ws:
        local.post("/api/admin/webserver/sessions/revoke-all")
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(50):
                ws.receive_text()
        assert closed.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as refused:
        with tablet.websocket_connect("/ws/admin", headers=cookie) as ws:
            ws.receive_text()
    assert refused.value.code == 4401


def test_an_old_pin_link_gives_no_access(local):
    allow_remote(local)
    assert remote(local).get(f"/api/admin/state?pin={PASSWORD}").status_code == 401


def test_the_login_page_only_returns_to_admin_pages():
    assert _safe_next("/admin/settings#webserver") == "/admin/settings#webserver"
    assert _safe_next("/qr") == "/qr"
    for target in (None, "", "https://evil.example", "//evil.example", "/admin//evil", "/admin\\evil", "/admin/login", "/qrx", "/"):
        assert _safe_next(target) == "/admin", target


def test_admin_qr_code_only_while_it_is_open(local):
    assert local.get("/api/qr.svg", params={"url": ADMIN_URL}).status_code == 404
    allow_remote(local)
    assert local.get("/api/qr.svg", params={"url": ADMIN_URL}).status_code == 200
    assert local.get("/api/admin/webserver").json()["admin_urls"][0]["url"] == ADMIN_URL


def test_changing_the_port(local):
    s = local.services.settings
    for port in (80, 70000, s.remote.local_port, s.libretranslate_port):
        assert local.post("/api/admin/webserver/port", json={"port": port}).status_code == 400, port
    assert local.post("/api/admin/webserver/port", json={"port": "abc"}).status_code == 400
    busy = socket.create_server(("0.0.0.0", 0))
    try:
        assert local.post("/api/admin/webserver/port", json={"port": busy.getsockname()[1]}).status_code == 409
    finally:
        busy.close()
    free = socket.create_server(("0.0.0.0", 0))
    port = free.getsockname()[1]
    free.close()
    web = local.post("/api/admin/webserver/port", json={"port": port}).json()
    assert web["saved_port"] == port and web["running_port"] == 8000
    assert local.services.settings.port == port
    assert ":8000/" in local.services.listener_url()  # the QR code keeps working until the restart


def test_restarting_for_a_new_port(local):
    services = local.services
    assert local.post("/api/admin/webserver/restart").status_code == 503
    restarts = []
    services.restart = lambda: restarts.append(True)
    services.pipeline.state = "running"
    assert local.post("/api/admin/webserver/restart").status_code == 409
    services.pipeline.state = "stopped"
    assert local.post("/api/admin/webserver/restart").status_code == 200
    time.sleep(0.7)
    local.get("/api/public")  # the app's loop runs the delayed restart
    assert restarts == [True]


def test_firewall_rule_update_is_reported(local, monkeypatch):
    monkeypatch.setattr("livetranslate.firewall.update_rule", lambda port: "cancelled")
    assert local.post("/api/admin/webserver/firewall").json()["firewall_change"] == "cancelled"
    local.services.firewall_problem = True
    monkeypatch.setattr("livetranslate.firewall.update_rule", lambda port: "ok")
    assert local.post("/api/admin/webserver/firewall").json()["firewall_change"] == "ok"
    assert local.services.firewall_problem is False
    assert local.get("/api/admin/state").json()["firewall_problem"] is False


@pytest.mark.parametrize("pin, short", [("1234", True), ("12345678", False)])
def test_an_admin_pin_becomes_the_password(tmp_path, pin, short):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**Settings().model_dump(exclude={"web_server"}), "admin_pin": pin}), "utf-8")
    store = SettingsStore(path)
    ws = store.settings.web_server
    assert ws.lan_admin is True and ws.password_short is short
    assert check_password(pin, ws.password_hash)
    on_disk = json.loads(path.read_text("utf-8"))
    assert "admin_pin" not in on_disk and pin not in json.dumps(on_disk["web_server"]).replace(ws.password_hash, "")


def test_no_admin_pin_leaves_it_off(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**Settings().model_dump(exclude={"web_server"}), "admin_pin": ""}), "utf-8")
    store = SettingsStore(path)
    assert store.settings.web_server.lan_admin is False and not store.settings.web_server.password_hash
    assert "admin_pin" not in path.read_text("utf-8")
