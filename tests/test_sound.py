import json

import numpy as np

from livetranslate.calibration import analyse, audio_clip, best_level
from livetranslate.servicelog import ServiceLog, prune
from livetranslate.stt.sound import SONG_LEVELS, SingingGate, SoundClassifier, SoundScores

SONG = SoundScores(speech=0.1, singing=0.6, music=0.8)
PRAYER_OVER_PIANO = SoundScores(speech=0.7, singing=0.1, music=0.6)
UNSURE = SoundScores(speech=0.5, singing=0.4, music=0.5)


def test_gate_needs_two_passes_and_lets_speech_over_music_through():
    gate = SingingGate()
    assert gate.update(SONG) is False  # one pass isn't enough
    assert gate.update(SONG) is True
    assert gate.update(PRAYER_OVER_PIANO) is True  # nor to leave a song
    assert gate.update(PRAYER_OVER_PIANO) is False
    assert gate.update(PRAYER_OVER_PIANO) is False


def test_worship_music_counts_as_a_song_without_any_singing_score():
    # What a band with a congregation really scored in a service.
    worship = SoundScores(speech=0.0, singing=0.03, music=0.85)
    gate = SingingGate()
    gate.update(worship)
    assert gate.update(worship) is True


def test_a_steady_voice_gets_through_loud_music():
    # Speech under loud music can score low on "Speech"; the voice detector still hears it.
    prayer = SoundScores(speech=0.1, singing=0.02, music=0.8)
    gate = SingingGate()
    assert not any(gate.update(prayer, voiced=0.7) for _ in range(5))
    # A song does trigger the voice detector now and then, which mustn't let it through.
    gate = SingingGate()
    results = [gate.update(prayer, voiced=v) for v in (0.0, 0.0, 0.0, 0.5, 0.0, 0.0)]
    assert all(results[1:])


def test_gate_translates_when_unsure():
    gate = SingingGate()
    assert not any(gate.update(UNSURE) for _ in range(5))


def test_gate_ignores_a_single_stray_pass():
    gate = SingingGate()
    gate.update(SONG)
    gate.update(PRAYER_OVER_PIANO)
    assert gate.update(SONG) is False


def test_levels_move_the_gate_from_translating_to_skipping():
    # Fairly quiet music: the default translates it, "skip more" doesn't.
    quiet = SoundScores(speech=0.05, singing=0.02, music=0.36)
    assert not any(SingingGate().update(quiet) for _ in range(3))
    gate = SingingGate(SONG_LEVELS[1])
    assert [gate.update(quiet) for _ in range(2)] == [False, True]
    # Clear music: the far "translate more" end still needs a steady voice to let it through.
    worship = SoundScores(speech=0.0, singing=0.03, music=0.85)
    gate = SingingGate(SONG_LEVELS[-3])
    assert not any(gate.update(worship, voiced=0.3) for _ in range(5))
    gate = SingingGate()
    assert any(gate.update(worship, voiced=0.3) for _ in range(5))


def test_levels_only_ever_lean_one_way():
    levels = [SONG_LEVELS[n] for n in sorted(SONG_LEVELS)]
    for easier, harder in zip(levels[1:], levels):
        assert easier.min_music < harder.min_music
        assert easier.max_speech > harder.max_speech
        assert easier.max_voiced > harder.max_voiced


def service_log(tmp_path, events):
    folder = tmp_path / "2026-11-01_09-30-00"
    folder.mkdir()
    lines = "".join(json.dumps(e) + "\n" for e in events)
    (folder / "events.jsonl").write_text(lines + '{"t": 99, "cut off', "utf-8")  # a crash can leave half a line
    return folder


def sound(t, speech=0.0, music=0.85, voiced=0.0):
    return {"t": t, "kind": "sound", "speech": speech, "singing": 0.03, "music": music, "voiced": voiced}


def test_analysis_replays_the_decisions_for_every_level(tmp_path):
    speech = [sound(t, speech=0.8, music=0.1, voiced=0.9) for t in range(0, 10)]
    song = [sound(t) for t in range(10, 40)]
    after = [sound(t, speech=0.8, music=0.1, voiced=0.9) for t in range(40, 50)]
    events = [*speech, {"t": 5, "kind": "sentence", "text": "Welkom"}, *song, *after, {"t": 45, "kind": "mark"}]
    result = analyse(service_log(tmp_path, events))
    assert result["levels"][0]["skipped"] == [[12, 41]]  # it takes a few passes to notice and to let go
    assert result["levels"][0]["skipped_s"] == 29
    assert result["marks"] == [45] and result["sentences"] == [{"t": 5, "text": "Welkom"}]
    assert set(result["levels"]) == set(SONG_LEVELS)


def test_a_pause_ends_a_song_in_the_replay(tmp_path):
    before = [sound(t) for t in range(0, 10)]
    after = [sound(t) for t in range(300, 310)]
    result = analyse(service_log(tmp_path, [*before, *after]))
    assert len(result["levels"][0]["skipped"]) == 2
    assert result["levels"][0]["skipped_s"] < 20


def test_best_level_follows_the_labels_and_prefers_translating():
    # Levels that skip 0-100, 0-50 and nothing.
    def level(*skipped):
        return {"skipped": [list(s) for s in skipped], "skipped_s": 0, "speech_s": 0}

    analysis = {"levels": {n: level() for n in SONG_LEVELS}}
    analysis["levels"][0] = level((0, 50))
    analysis["levels"][1] = level((0, 100))
    music = {"start": 0, "end": 100, "kind": "music"}
    speech = {"start": 50, "end": 100, "kind": "speech"}
    assert best_level(analysis, []) == 0
    assert best_level(analysis, [music]) == 1
    assert best_level(analysis, [{**music, "end": 50}, speech]) == 0
    assert best_level(analysis, [{"start": 0, "end": 50, "kind": "speech"}]) == -1  # nearest that skips none of it


def test_audio_clip_stays_in_step_after_a_pause(tmp_path):
    import io

    import soundfile as sf

    folder = service_log(tmp_path, [{"t": 10, "kind": "paused"}, {"t": 40, "kind": "resumed"}])
    # One tone per second: the clip's first sample tells where it starts.
    audio = np.repeat(np.arange(60, dtype=np.float32) / 100, 16000)
    sf.write(folder / "audio.flac", audio, 16000, format="FLAC", subtype="PCM_16")
    clip, rate = sf.read(io.BytesIO(audio_clip(folder, 52.0)), dtype="float32")
    assert rate == 16000 and len(clip) == 20 * 16000
    assert abs(clip[0] - 0.20) < 0.001  # 52 s on the clock, less 30 s paused and 2 s lead


def test_calibration_api(tmp_path):
    from fastapi.testclient import TestClient

    from livetranslate.config import Settings, SettingsStore
    from livetranslate.server.app import create_app

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings())
    logs = tmp_path / "service-logs"
    logs.mkdir()
    folder = service_log(logs, [sound(t) for t in range(30)])
    other = logs / "no-sound"
    other.mkdir()
    (other / "events.jsonl").write_text('{"t": 1, "kind": "start"}\n', "utf-8")
    with TestClient(create_app(store, tmp_path, manage_translator=False)) as client:
        found = client.get("/api/admin/logs").json()
        assert [s["name"] for s in found] == [folder.name] and found[0]["audio"] is False
        analysis = client.get(f"/api/admin/logs/{folder.name}/analysis").json()
        assert analysis["levels"]["0"]["skipped"]
        label = {"start": 0, "end": 29, "kind": "music"}
        assert client.post(f"/api/admin/logs/{folder.name}/fit", json={"labels": [label]}).json()["level"] == 0
        assert client.get(f"/api/admin/logs/{folder.name}/clip?t=1").status_code == 404  # nothing recorded
        assert client.get("/api/admin/logs/nothing/analysis").status_code == 404
        assert client.get("/api/admin/logs/..%5C..%5Csecret/analysis").status_code == 404
        assert client.post("/api/admin/singing/level", json={"level": 2}).status_code == 200
        assert store.settings.song_level == 2
        assert client.post("/api/admin/singing/level", json={"level": 9}).status_code == 400
        assert store.settings.song_level == 2


def test_classifier_runs_on_the_cpu_and_pads_short_audio():
    classifier = SoundClassifier(gpu=False)
    silence = classifier.scores(np.zeros(4000, dtype=np.float32))
    assert 0 <= silence.speech < 0.2 and 0 <= silence.singing < 0.2
    t = np.arange(16000 * 2) / 16000
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    assert classifier.scores(tone).speech < 0.3


def test_service_log_writes_events_and_audio(tmp_path):
    import soundfile as sf

    log = ServiceLog(tmp_path, audio=True)
    log.event("sentence", text="Welkom in де kerk")
    log.audio(np.zeros(16000, dtype=np.float32))
    log.close()
    log.event("too late")  # after stopping: ignored
    lines = [json.loads(l) for l in (log.folder / "events.jsonl").read_text("utf-8").splitlines()]
    assert [l["kind"] for l in lines] == ["sentence"]
    assert lines[0]["text"] == "Welkom in де kerk"
    assert sf.info(str(log.folder / "audio.flac")).duration == 1.0


def test_service_log_without_audio_records_none(tmp_path):
    log = ServiceLog(tmp_path)
    log.audio(np.zeros(16000, dtype=np.float32))
    log.close()
    assert not (log.folder / "audio.flac").exists()


def test_old_logs_are_removed(tmp_path):
    import os

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    os.utime(old, (1, 1))
    prune(tmp_path)
    assert not old.exists() and new.exists()
