import pytest


@pytest.fixture(autouse=True)
def test_client_is_this_pc(monkeypatch):
    """Starlette's TestClient comes from "testclient" with Host "testserver". The app only counts
    real loopback addresses as this PC; in tests those two names count as well."""
    from livetranslate.server import app

    monkeypatch.setattr(app, "LOCAL_HOSTS", app.LOCAL_HOSTS | {"testclient"})
    monkeypatch.setattr(app, "LOCAL_NAMES", app.LOCAL_NAMES | {"testserver"})
