from livetranslate.tray import TEXTS, Tray, make_icon, status_text, tray_state


def test_tray_state_reflects_pipeline():
    base = {"state": "running", "paused": False, "speaker_language": "nl"}
    assert tray_state({**base, "state": "stopped"}, "nl") == "stopped"
    assert tray_state({**base, "state": "loading"}, "nl") == "loading"
    assert tray_state(base, "nl") == "running"
    assert tray_state({**base, "paused": True}, "nl") == "paused"
    assert tray_state({**base, "speaker_language": "ru"}, "nl") == "testimony"


def test_status_text_is_translated():
    assert status_text("running", 7, TEXTS["nl"]) == "Bezig · 7 luisteraars"
    assert status_text("stopped", 3, TEXTS["en"]) == "Stopped"
    assert all(set(t) == set(TEXTS["en"]) for t in TEXTS.values())


def test_icon_colour_changes_with_state():
    running, stopped = make_icon("running"), make_icon("stopped")
    assert running.size == (64, 64) and running.mode == "RGBA"
    assert running.getpixel((8, 32)) != stopped.getpixel((8, 32))


def test_exit_calls_back_and_stops_icon():
    calls = []

    class FakeIcon:
        def stop(self):
            calls.append("icon stopped")

    tray = Tray(lambda: ("running", 0), "http://a", "http://q", lambda: calls.append("exit"), language="en")
    tray.icon = FakeIcon()
    tray._exit()
    assert calls == ["exit", "icon stopped"]


def test_interface_language_follows_installer(monkeypatch):
    from livetranslate.languages import ui_language

    monkeypatch.setenv("LIVETRANSLATE_UI_LANG", "uk")
    assert ui_language() == "uk"
    assert Tray(lambda: ("stopped", 0), "", "", lambda: None).texts is TEXTS["uk"]
    monkeypatch.setenv("LIVETRANSLATE_UI_LANG", "xx")  # unknown: falls back to the OS locale
    assert ui_language() in TEXTS


def test_windowless_start_gets_writable_streams(monkeypatch):
    import sys

    from livetranslate.__main__ import ensure_streams

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert ensure_streams() is False
    sys.stderr.write("progress bar\r")  # what tqdm does during model downloads
    sys.stdout.close()
    sys.stderr.close()
