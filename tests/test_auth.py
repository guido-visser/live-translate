import json

from livetranslate.server.auth import SESSION_S, TOUCH_SAVE_S, LoginLimiter, SessionStore, check_password, hash_password


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_password_hash_checks_out_and_holds_no_password():
    stored = hash_password("correct horse")
    assert "correct horse" not in stored and stored.startswith("scrypt$")
    assert check_password("correct horse", stored)
    assert not check_password("correct hors", stored)
    assert hash_password("correct horse") != stored  # its own salt each time
    for broken in ("", "plain", "scrypt$1$2$3", "bcrypt$16384$8$1$AA$AA"):
        assert not check_password("correct horse", broken)


def test_a_session_slides_and_ends_30_days_after_last_use(tmp_path):
    clock = Clock()
    store = SessionStore(tmp_path / "sessions.json", clock)
    token, session_id = store.create("192.168.1.50", "Mozilla/5.0 (iPad)")
    assert store.check(token) == session_id
    assert store.check("not-a-token") is None and store.check(None) is None
    clock.now += SESSION_S - 10
    assert store.check(token) == session_id  # used again: 30 more days
    clock.now += SESSION_S - 10
    assert store.check(token) == session_id
    clock.now += SESSION_S
    assert store.check(token) is None
    assert store.list() == []


def test_sessions_survive_a_restart_but_only_as_hashes(tmp_path):
    clock = Clock()
    path = tmp_path / "sessions.json"
    token, session_id = SessionStore(path, clock).create("192.168.1.50", "Firefox")
    assert token not in path.read_text("utf-8")
    again = SessionStore(path, clock)
    assert again.check(token) == session_id
    assert [s["ip"] for s in again.list()] == ["192.168.1.50"]


def test_last_seen_is_not_written_on_every_request(tmp_path):
    clock = Clock()
    path = tmp_path / "sessions.json"
    store = SessionStore(path, clock)
    token, _ = store.create("192.168.1.50", "")
    written = path.stat().st_mtime_ns, path.read_text("utf-8")
    clock.now += 5
    store.check(token)
    assert path.read_text("utf-8") == written[1]
    clock.now += TOUCH_SAVE_S
    store.check(token)
    saved = json.loads(path.read_text("utf-8"))["sessions"]
    assert next(iter(saved.values()))["last_seen"] == clock.now


def test_signing_out_one_or_all_devices(tmp_path):
    store = SessionStore(tmp_path / "sessions.json", Clock())
    tablet, tablet_id = store.create("192.168.1.50", "")
    laptop, _ = store.create("192.168.1.51", "")
    assert store.revoke(tablet_id) and not store.revoke(tablet_id)
    assert store.check(tablet) is None and store.check(laptop)
    store.revoke_all()
    assert store.check(laptop) is None
    assert SessionStore(tmp_path / "sessions.json").list() == []


def test_guessing_blocks_a_device_and_then_everyone():
    clock = Clock()
    limiter = LoginLimiter(clock)
    for _ in range(LoginLimiter.PER_DEVICE - 1):
        limiter.failed("10.0.0.1")
    assert limiter.blocked_for("10.0.0.1") == 0
    limiter.failed("10.0.0.1")
    assert 0 < limiter.blocked_for("10.0.0.1") <= LoginLimiter.BLOCK_S
    assert limiter.blocked_for("10.0.0.2") == 0
    clock.now += LoginLimiter.BLOCK_S
    assert limiter.blocked_for("10.0.0.1") == 0

    # Old failures fall out of the window.
    limiter = LoginLimiter(clock)
    for _ in range(LoginLimiter.PER_DEVICE - 1):
        limiter.failed("10.0.0.1")
    clock.now += LoginLimiter.WINDOW_S
    limiter.failed("10.0.0.1")
    assert limiter.blocked_for("10.0.0.1") == 0

    # Many devices together.
    limiter = LoginLimiter(clock)
    for i in range(LoginLimiter.TOTAL):
        limiter.failed(f"10.0.1.{i}")
    assert limiter.blocked_for("10.0.2.1") > 0


def test_a_right_password_forgets_the_wrong_ones():
    limiter = LoginLimiter(Clock())
    for _ in range(LoginLimiter.PER_DEVICE - 1):
        limiter.failed("10.0.0.1")
    limiter.succeeded("10.0.0.1")
    limiter.failed("10.0.0.1")
    assert limiter.blocked_for("10.0.0.1") == 0
