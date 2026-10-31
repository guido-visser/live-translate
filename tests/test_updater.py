import asyncio
import hashlib

import httpx
import pytest

from livetranslate import updater
from livetranslate.updater import Updater, parse_version, release_info

INSTALLER = b"fake installer bytes"

RELEASE = {
    "tag_name": "v9.0.1",
    "html_url": "https://github.com/guido-visser/live-translate/releases/tag/v9.0.1",
    "assets": [
        {"name": "notes.txt", "browser_download_url": "https://example.test/notes.txt"},
        {
            "name": "LiveTranslate-Setup-9.0.1.exe",
            "browser_download_url": "https://example.test/LiveTranslate-Setup-9.0.1.exe",
            "size": len(INSTALLER),
            "digest": "sha256:" + hashlib.sha256(INSTALLER).hexdigest(),
        },
    ],
}


def test_parse_version():
    assert parse_version("v0.1.10") > parse_version("0.1.9")
    assert parse_version("0.2") > parse_version("0.1.99")
    assert parse_version("1.0.0-rc1") == (1, 0, 0)
    assert parse_version("nonsense") == ()


def test_release_info_finds_the_installer():
    info = release_info(RELEASE)
    assert info["version"] == "9.0.1"
    assert info["asset"] == "LiveTranslate-Setup-9.0.1.exe"
    assert info["sha256"] == hashlib.sha256(INSTALLER).hexdigest()
    assert release_info({"tag_name": "v1.0.0", "assets": []})["asset"] is None


@pytest.fixture
def fake_github(monkeypatch):
    """Route the updater's httpx clients to a fake GitHub."""
    body = {"installer": INSTALLER}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == updater.LATEST_URL:
            return httpx.Response(200, json=RELEASE)
        if request.url.path.endswith(".exe"):
            return httpx.Response(200, content=body["installer"])
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(updater.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return body


def make_updater(tmp_path, monkeypatch, idle=True):
    started, exited = [], []
    monkeypatch.setattr(updater, "run_installer", started.append)
    u = Updater(tmp_path, lambda: idle, lambda: exited.append(True))
    u.current = "0.1.4"
    u.can_install = True
    return u, started, exited


async def test_check_reports_newer_version(tmp_path, monkeypatch, fake_github):
    u, _, _ = make_updater(tmp_path, monkeypatch)
    status = await u.check()
    assert status["available"] and status["latest"] == "9.0.1" and status["can_install"]

    u.current = "9.0.1"
    assert not u.status()["available"]


async def test_check_without_internet_is_quiet(tmp_path, monkeypatch):
    def offline(request):
        raise httpx.ConnectError("no internet")

    real = httpx.AsyncClient
    monkeypatch.setattr(updater.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(offline), **kw))
    u, _, _ = make_updater(tmp_path, monkeypatch)
    status = await u.check()
    assert status["state"] == "error" and not status["available"]


async def test_install_downloads_verifies_runs_and_exits(tmp_path, monkeypatch, fake_github):
    u, started, exited = make_updater(tmp_path, monkeypatch)
    await u.check()
    await u.install()
    assert started == [tmp_path / "updates" / "LiveTranslate-Setup-9.0.1.exe"]
    assert started[0].read_bytes() == INSTALLER
    assert u.state == "installing"
    await asyncio.sleep(1.1)
    assert exited == [True]


async def test_install_rejects_a_damaged_download(tmp_path, monkeypatch, fake_github):
    fake_github["installer"] = b"tampered"
    u, started, _ = make_updater(tmp_path, monkeypatch)
    await u.check()
    with pytest.raises(RuntimeError, match="checksum"):
        await u.install()
    assert not started and u.state == "error"
    assert not list((tmp_path / "updates").iterdir())


async def test_no_install_while_translating(tmp_path, monkeypatch, fake_github):
    u, started, _ = make_updater(tmp_path, monkeypatch, idle=False)
    await u.check()
    with pytest.raises(RuntimeError, match="Stop translating"):
        await u.install()
    assert not started
