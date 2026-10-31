import subprocess

import pytest

from livetranslate import native
from livetranslate.config import Settings
from livetranslate.translate import libretranslate
from livetranslate.translate.libretranslate import LibreTranslate, _first_error
from livetranslate.tts.piper import VoiceBank

OLD_RUNTIME = (14, 32, 31332, 0)


def test_missing_module_is_reported_without_crashing():
    error = native._probe("no_such_module_here")
    assert error.startswith("no_such_module_here cannot be loaded (ModuleNotFoundError")


def test_crash_on_import_explains_the_old_runtime(monkeypatch):
    crashed = subprocess.CompletedProcess([], returncode=-1073741819, stdout="", stderr="")
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: crashed)
    monkeypatch.setattr(native, "vc_runtime_version", lambda: OLD_RUNTIME)
    error = native._probe("onnxruntime")
    assert "crashed with an access violation" in error
    assert "14.32.31332" in error and native.VC_REDIST_URL in error


def test_recent_runtime_gives_no_hint(monkeypatch):
    monkeypatch.setattr(native, "vc_runtime_version", lambda: (14, 44, 35211, 0))
    assert native.runtime_hint() == ""
    assert not native.vc_runtime_outdated()


def test_broken_onnxruntime_disables_voices_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "_onnx_error", "onnxruntime cannot be loaded (crashed).")
    bank = VoiceBank(tmp_path)
    assert not bank.has_voice("ru")
    with pytest.raises(RuntimeError, match="onnxruntime cannot be loaded"):
        bank._voice("ru")


def test_libretranslate_crash_message(monkeypatch, tmp_path):
    lt = LibreTranslate(Settings(), tmp_path)
    monkeypatch.setattr(libretranslate, "runtime_hint", lambda: "Update the runtime.")
    assert lt._exit_message(3221225477) == "LibreTranslate crashed while loading (access violation). Update the runtime."
    lt.log_path.write_text("loading\nTraceback\nValueError: bad model\n", "utf-8")
    assert lt._exit_message(1) == "LibreTranslate exited with code 1. Last output: loading | Traceback | ValueError: bad model"


def test_install_error_shows_the_error_line_not_the_last_packages():
    output = "Resolved 80 packages\n  x Failed to build `sentencepiece==0.2.2`\nerror: build backend failed\n + tqdm==4.67.1\n"
    assert _first_error(output) == "x Failed to build `sentencepiece==0.2.2`"
    assert _first_error(" + tqdm==4.67.1\n") == "+ tqdm==4.67.1"
