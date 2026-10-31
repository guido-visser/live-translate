import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

from livetranslate import tunnel as tunnel_module
from livetranslate.config import RemoteSettings, Settings, SettingsStore
from livetranslate.server.app import create_app
from livetranslate.tunnel import Tunnel, explain

REMOTE = RemoteSettings(
    enabled=True,
    public_url="https://vertaling.example.nl/",
    ssh_host="vps.example.nl",
    ssh_user="livetranslate",
    ssh_port=2222,
    remote_port=8800,
    local_port=8001,
)


def make_client(tmp_path, local_port: int) -> TestClient:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(tts_enabled=False, remote=REMOTE.model_copy(update={"local_port": local_port})))
    return TestClient(create_app(store, tmp_path, manage_translator=False))


def test_over_the_tunnel_only_the_listener_page_is_served(tmp_path):
    # TestClient requests arrive on port 80: pretend that is the tunnel port.
    with make_client(tmp_path, local_port=80) as c:
        assert c.get("/").status_code == 200
        assert c.get("/static/client/app.js").status_code == 200
        assert c.get("/api/public").json()["url"] == "https://vertaling.example.nl/"
        for path in ("/admin", "/admin/settings", "/api/admin/state", "/qr", "/static/admin/settings.js"):
            assert c.get(path).status_code == 403, path
        assert c.post("/api/admin/start").status_code == 403
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/admin"):
                pass
        with c.websocket_connect("/ws/listen"):
            pass


def test_on_the_normal_port_the_operator_page_still_works(tmp_path):
    with make_client(tmp_path, local_port=8001) as c:
        assert c.get("/api/admin/state").status_code == 200
        state = c.get("/api/admin/state").json()
        assert state["url"] == "https://vertaling.example.nl/"  # the QR code shows the public address
        assert state["tunnel"]["state"] == "off"  # not started without manage_translator


def test_saving_keeps_the_bound_tunnel_port(tmp_path):
    with make_client(tmp_path, local_port=8001) as c:
        settings = c.get("/api/admin/state").json()["settings"]
        settings["remote"]["enabled"] = False
        settings["remote"]["local_port"] = 9999
        saved = c.post("/api/admin/settings", json=settings).json()["settings"]["remote"]
        assert saved["local_port"] == 8001 and not saved["enabled"]


def test_ssh_command(tmp_path):
    t = Tunnel(tmp_path, 8001)
    t.settings = REMOTE
    cmd = t.command()
    assert cmd[-1] == "livetranslate@vps.example.nl"
    assert cmd[cmd.index("-R") + 1] == "127.0.0.1:8800:127.0.0.1:8001"
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in cmd and "ExitOnForwardFailure=yes" in cmd


def test_ssh_errors_are_explained():
    assert "authorized_keys of livetranslate" in explain("livetranslate@vps: Permission denied (publickey).", REMOTE)
    assert "Port 8800" in explain("Error: remote port forwarding failed for listen port 8800", REMOTE)
    assert explain("Warning: Permanently added 'vps' to the list of known hosts.\nssh: connect to host vps port 22: Connection refused", REMOTE) == "ssh: connect to host vps port 22: Connection refused"


async def run_tunnel(tmp_path, monkeypatch, script: str, wait: float) -> Tunnel:
    """Run the supervisor with a stand-in for ssh."""
    t = Tunnel(tmp_path, 8001)
    (tmp_path / "tunnel_key.pub").write_text("ssh-ed25519 AAAA test", "utf-8")
    monkeypatch.setattr(tunnel_module, "ssh_executable", lambda name="ssh": sys.executable)
    monkeypatch.setattr(tunnel_module, "CONNECTED_AFTER_S", 0.5)
    monkeypatch.setattr(Tunnel, "command", lambda self: [sys.executable, "-c", script])
    await t.apply(REMOTE.model_copy(update={"public_url": ""}))
    await asyncio.sleep(wait)
    return t


async def test_tunnel_reports_a_refused_key(tmp_path, monkeypatch):
    t = await run_tunnel(tmp_path, monkeypatch, "import sys; sys.stderr.write('Permission denied (publickey).\\n'); sys.exit(255)", 1.5)
    assert t.state == "error" and "refused this PC's key" in t.error
    await t.stop()
    assert t.state == "off"


async def test_tunnel_that_stays_up_is_connected(tmp_path, monkeypatch):
    t = await run_tunnel(tmp_path, monkeypatch, "import time; time.sleep(30)", 1.5)
    assert t.state == "connected" and t.error == ""
    await t.stop()
    assert t.state == "off" and t._proc is None


async def test_missing_settings_are_reported(tmp_path):
    t = Tunnel(tmp_path, 8001)
    await t.apply(RemoteSettings(enabled=True))
    await asyncio.sleep(0.1)
    assert t.state == "error" and "server and the user" in t.error
    await t.stop()
