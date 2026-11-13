import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from livetranslate import training
from livetranslate.config import Settings, SettingsStore, SongModel
from livetranslate.server.app import create_app
from livetranslate.stt.sound import SingingGate, song_chance
from livetranslate.training import (
    MIXED,
    SONG,
    SPEECH,
    RUN_S,
    Sample,
    Trainer,
    clear_stretches,
    compare,
    fit,
    is_youtube,
    learn,
    list_youtube,
    score,
    by_model,
    by_rules,
    verdict,
    youtube_video_id,
)

SPEECH_ROW = [0.8, 0.0, 0.0, 0.7]
SONG_ROW = [0.0, 0.03, 0.85, 0.05]
MIXED_ROW = [0.2, 0.0, 0.7, 0.2]


def rows(*parts):
    return np.array([row for row, seconds in parts for _ in range(seconds)], dtype=np.float32)


def test_only_long_and_clear_stretches_become_examples():
    service = rows(
        ([0, 0, 0, 0], 100),  # silence: neither
        (SONG_ROW, 300),
        (SPEECH_ROW, 400),
        (MIXED_ROW, 200),  # someone talking over the band: too doubtful to learn from as either
        (SONG_ROW, 40),  # too short
        (SPEECH_ROW, 300),
    )
    songs, talks = clear_stretches(service)
    assert len(songs) == 1 and len(talks) == 2
    a, b = songs[0]
    assert 100 < a < b < 400  # stays away from the edges
    assert all(400 < a < b for a, b in talks)
    assert all(not 800 <= a < 1000 for a, _ in talks)


def test_a_service_without_songs_gives_no_songs():
    songs, talks = clear_stretches(rows((SPEECH_ROW, 1200)))
    assert songs == [] and len(talks) == 1


def fake_sample(seed: int) -> Sample:
    """Songs and speech that differ in one direction, and mixes that lean towards speech."""
    rng = np.random.default_rng(seed)
    runs = []
    for run, (kind, center, row) in enumerate([(SONG, 2.0, SONG_ROW)] * 10 + [(SPEECH, -2.0, SPEECH_ROW)] * 10 + [(MIXED, -1.0, MIXED_ROW)] * 4):
        description = rng.normal(0, 0.3, (RUN_S, 1024)).astype(np.float32)
        description[:, 0] += center
        runs.append((kind, run, description, np.tile(np.array(row, dtype=np.float32), (RUN_S, 1))))
    return Sample(
        descriptions=np.concatenate([r[2] for r in runs]),
        rows=np.concatenate([r[3] for r in runs]),
        kinds=np.concatenate([np.full(RUN_S, r[0], dtype=np.int8) for r in runs]),
        runs=np.concatenate([np.full(RUN_S, r[1], dtype=np.int32) for r in runs]),
        mixes=np.zeros(RUN_S * len(runs), dtype=np.int8),
    )


def test_fit_finds_what_tells_the_two_apart():
    x = np.random.default_rng(0).normal(0, 1, (400, 1024)).astype(np.float32)
    y = np.r_[np.ones(200), np.zeros(200)]
    x[:200, 7] += 3
    x[200:, 7] -= 3
    weights, bias = fit(x, y, np.ones(400))
    assert song_chance(weights, bias, x[:200].mean(axis=0)) > 0.9
    assert song_chance(weights, bias, x[200:].mean(axis=0)) < 0.1


def test_learned_model_beats_the_rules_on_a_service_it_has_not_seen():
    weights, bias = learn([fake_sample(1), fake_sample(2)])
    unseen = [fake_sample(3)]
    trained, rules = score(by_model(weights, bias), unseen), score(by_rules(0), unseen)
    assert trained["song"] == 0 and trained["speech"] == 0
    assert trained["mixed"] == 0
    assert rules["mixed"] > 50  # the rules take this for a song


def test_score_ignores_the_passes_before_a_decision_can_be_made():
    # Every run starts undecided, which the score must not count as a song let through.
    sample = fake_sample(1)
    result = score(by_rules(0), [sample])
    assert result["song"] == 0 and result["speech"] == 0


def test_comparison_keeps_the_tested_service_out_of_the_learning():
    seen = []
    real = training.learn

    def spying(samples):
        seen.append(samples)
        return real(samples)

    training.learn = spying
    try:
        samples = [fake_sample(i) for i in range(3)]
        result = compare(samples, 0)
    finally:
        training.learn = real
    assert set(result) == {"rules", "trained"}
    assert len(seen) == 3 and all(len(s) == 2 for s in seen)
    for i, used in enumerate(seen):
        assert not any(s is samples[i] for s in used)


@pytest.mark.parametrize(
    "trained, problem",
    [
        ({"song": 3, "speech": 0.2, "mixed": 10}, ""),
        ({"song": 3, "speech": 1.5, "mixed": 10}, "speech"),
        ({"song": 6, "speech": 0.2, "mixed": 10}, ""),  # a little more song, for much less speech lost
        ({"song": 8, "speech": 0.2, "mixed": 10}, "song"),
        ({"song": 3, "speech": 0.2, "mixed": 30}, "mixed"),
    ],
)
def test_a_new_model_only_takes_over_when_it_is_not_worse(trained, problem):
    assert verdict({"rules": {"song": 5, "speech": 0, "mixed": 25}, "trained": trained}) == problem


def test_gate_takes_a_verdict_from_anyone():
    gate = SingingGate()
    assert gate.decide(True) is False  # one pass isn't enough
    assert gate.decide(True) is True
    assert gate.decide(False) is True
    assert gate.decide(False) is False


@pytest.mark.parametrize(
    "link, video",
    [
        ("https://www.youtube.com/watch?v=sBNAkeR3JvQ", "sBNAkeR3JvQ"),
        ("https://www.youtube.com/watch?v=sBNAkeR3JvQ&list=PL123", "sBNAkeR3JvQ"),
        ("https://www.youtube.com/live/RSI_sLJyC-w", "RSI_sLJyC-w"),
        ("https://www.youtube.com/live/RSI_sLJyC-w?si=abc", "RSI_sLJyC-w"),
        ("https://youtu.be/sBNAkeR3JvQ", "sBNAkeR3JvQ"),
        ("https://www.youtube.com/channel/UCG6l_GZz9NpJneHPEN-sovw", None),
        ("https://www.youtube.com/@somechurch/streams", None),
        ("https://www.youtube.com/playlist?list=PL123", None),
    ],
)
def test_links_to_a_video_and_to_many(link, video):
    assert is_youtube(link)
    assert youtube_video_id(link) == video


def test_only_youtube_is_a_link():
    assert not is_youtube("https://evil.example/watch?v=abcdefgh")
    assert not is_youtube("file:///C:/secret")
    assert not is_youtube(r"C:\services\zondag.mp4")


def test_listing_flattens_a_channel_and_leaves_shorts_out(monkeypatch):
    channel = {
        "entries": [
            {"entries": [
                {"id": "aaaaaaaaaaa", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Dienst", "duration": 6000},
                {"id": "aaaaaaaaaaa", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Dienst", "duration": 6000},
                {"id": "bbbbbbbbbbb", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "Kort", "duration": 2200},
            ]},
            {"id": "ccccccccccc", "url": "https://www.youtube.com/shorts/ccccccccccc", "title": "Short", "duration": 30},
            {"id": "ddddddddddd", "url": "https://www.youtube.com/watch?v=ddddddddddd", "title": "Onbekend"},
        ]
    }

    class Fake:
        def __init__(self, options):
            assert options["extract_flat"] and "logger" in options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download):
            assert download is False
            return channel

    monkeypatch.setattr("yt_dlp.YoutubeDL", Fake)
    videos = list_youtube("https://www.youtube.com/@church")
    assert [v["title"] for v in videos] == ["Dienst", "Kort", "Onbekend"]
    assert [v["suggested"] for v in videos] == [True, False, True]  # short ones aren't ticked


def make_trainer(tmp_path, may_run=lambda: True, model=None):
    return Trainer(tmp_path, lambda: Settings(), may_run, model or (lambda m: None), lambda: False)


def wait_until_idle(trainer, seconds=30):
    end = time.time() + seconds
    while trainer.state != "idle" and time.time() < end:
        time.sleep(0.01)
    assert trainer.state == "idle"


def test_adding_sorts_videos_channels_files_and_mistakes(tmp_path):
    trainer = make_trainer(tmp_path)
    recording = tmp_path / "zondag.mp4"
    recording.write_bytes(b"x")
    answer = trainer.add(
        "https://youtu.be/sBNAkeR3JvQ\n"
        "https://www.youtube.com/watch?v=sBNAkeR3JvQ\n\n"  # the same one again
        "https://www.youtube.com/@church\n"
        f'"{recording}"\n'
        "C:/does/not/exist.mp4\n"
        "https://evil.example/video\n"
    )
    assert answer["added"] == 2
    assert answer["channels"] == ["https://www.youtube.com/@church"]
    assert answer["rejected"] == ["C:/does/not/exist.mp4", "https://evil.example/video"]
    assert [s["kind"] for s in trainer.status()["sources"]] == ["youtube", "file"]
    # And it's all still there after a restart.
    assert len(make_trainer(tmp_path).sources) == 2


def test_add_listed_refuses_what_is_not_a_video(tmp_path):
    trainer = make_trainer(tmp_path)
    added = trainer.add_listed([{"source": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "Dienst"}, {"source": "C:/secret.mp4", "title": "x"}])
    assert added == 1 and trainer.sources[0]["title"] == "Dienst"


def save_sample(trainer, source_id, seed):
    sample = fake_sample(seed)
    trainer.folder.mkdir(exist_ok=True)
    np.savez_compressed(trainer._sample_file(source_id), descriptions=sample.descriptions, rows=sample.rows, kinds=sample.kinds, runs=sample.runs, mixes=sample.mixes)
    trainer.sources.append({"id": source_id, "kind": "file", "source": source_id, "title": source_id, "state": "done", "detail": "", "song_s": 600, "speech_s": 900})


def test_training_on_what_was_read_puts_the_model_to_use(tmp_path):
    models = []
    trainer = make_trainer(tmp_path, model=models.append)
    save_sample(trainer, "a", 1)
    save_sample(trainer, "b", 2)
    assert trainer.start()
    wait_until_idle(trainer)
    status = trainer.status()
    assert status["error"] == "" and status["result"]["used"] is True and status["result"]["services"] == 2
    assert len(models) == 1 and models[0].services == 2 and len(models[0].weights) == 1024
    assert json.loads((trainer.folder / "result.json").read_text("utf-8"))["used"] is True
    # A model is small enough to travel in a church profile.
    assert len(models[0].model_dump_json()) < 20000


def test_one_service_is_not_enough_to_test_on(tmp_path):
    models = []
    trainer = make_trainer(tmp_path, model=models.append)
    save_sample(trainer, "a", 1)
    assert trainer.start()
    wait_until_idle(trainer)
    assert models == [] and trainer.status()["result"] is None and trainer.status()["usable"] == 1


def test_training_gives_way_to_a_service(tmp_path):
    running = []
    trainer = make_trainer(tmp_path, may_run=lambda: not running)
    running.append(True)
    assert trainer.start() is False  # not during a service
    assert trainer.state == "idle" and trainer.status()["may_run"] is False


def test_starting_a_service_stops_training_and_keeps_the_place(tmp_path, monkeypatch):
    running = []
    trainer = make_trainer(tmp_path, may_run=lambda: not running)
    recording = tmp_path / "zondag.mp4"
    recording.write_bytes(b"x")
    trainer.add(str(recording))
    monkeypatch.setattr("faster_whisper.decode_audio", lambda *a, **k: np.zeros(16000 * 5, dtype=np.float32))
    monkeypatch.setattr(training, "SoundClient", lambda gpu: object())

    def sampling(*_a, **_k):
        running.append(True)  # the operator presses start now
        trainer._check()

    monkeypatch.setattr(training, "sample_service", sampling)
    assert trainer.start()
    wait_until_idle(trainer)
    status = trainer.status()
    assert status["stopped_for_service"] is True
    assert status["sources"][0]["state"] == "waiting"  # it goes on from here next time


def test_one_bad_recording_does_not_stop_the_others(tmp_path, monkeypatch):
    trainer = make_trainer(tmp_path)
    for name in ("bad.mp4", "good.mp4"):
        (tmp_path / name).write_bytes(b"x")
        trainer.add(str(tmp_path / name))
    monkeypatch.setattr(training, "SoundClient", lambda gpu: object())

    def decoding(path, sampling_rate):
        if path.endswith("bad.mp4"):
            raise ValueError("Not a recording")
        return np.zeros(16000, dtype=np.float32)

    monkeypatch.setattr("faster_whisper.decode_audio", decoding)
    monkeypatch.setattr(training, "sample_service", lambda *a, **k: (None, 10, 20))
    assert trainer.start()
    wait_until_idle(trainer)
    bad, good = trainer.status()["sources"]
    assert bad["state"] == "error" and "Not a recording" in bad["detail"]
    assert good["state"] == "skipped" and good["detail"] == "unclear" and good["song_s"] == 10


def test_removing_forgets_what_was_read(tmp_path):
    trainer = make_trainer(tmp_path)
    save_sample(trainer, "a", 1)
    file = trainer._sample_file("a")
    assert file.exists()
    trainer.remove("a")
    assert trainer.sources == [] and not file.exists()


@pytest.fixture
def client(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings())
    with TestClient(create_app(store, tmp_path, manage_translator=False)) as test_client:
        test_client.store = store
        yield test_client


MODEL = {"weights": [0.0] * 1024, "bias": 1.0, "services": 3, "trained_at": "2026-11-04"}


def test_training_api_adds_and_removes(client, tmp_path):
    assert client.get("/api/admin/training").json()["sources"] == []
    answer = client.post("/api/admin/training/add", json={"text": "https://youtu.be/sBNAkeR3JvQ\nnonsense"}).json()
    assert answer["added"] == 1 and answer["rejected"] == ["nonsense"]
    status = client.get("/api/admin/training").json()
    assert status["may_run"] is True and status["needed"] == 2
    source = status["sources"][0]["id"]
    assert client.delete(f"/api/admin/training/sources/{source}").json()["sources"] == []
    assert client.post("/api/admin/training/list", json={"url": "https://evil.example/x"}).status_code == 400


def test_a_page_saving_settings_can_not_undo_training(client):
    assert client.post("/api/admin/training/model", json={"model": MODEL}).status_code == 200
    assert client.store.settings.song_model.services == 3
    state = client.get("/api/admin/state").json()
    state["settings"]["song_model"] = None  # a page that was opened before the training finished
    client.post("/api/admin/settings", json=state["settings"])
    assert client.store.settings.song_model is not None
    assert client.post("/api/admin/training/model", json={"model": None}).json()["settings"]["song_model"] is None


def test_a_model_travels_in_the_church_profile(client):
    client.post("/api/admin/training/model", json={"model": MODEL})
    profile = client.get("/api/admin/profile").json()
    assert profile["song_model"]["services"] == 3
    assert "song_model" in client.get("/api/admin/state").json()["profile_fields"]


def test_a_model_from_a_profile_is_checked(client):
    assert client.post("/api/admin/training/model", json={"model": {"weights": [1.0], "bias": 0}}).status_code == 400
    assert client.store.settings.song_model is None
    SongModel.model_validate(MODEL)


def test_hundreds_of_services_are_thinned_but_keep_every_kind(tmp_path):
    trainer = make_trainer(tmp_path)
    for i in range(2):
        save_sample(trainer, f"s{i}", i)
    full = trainer._read_sample(trainer.sources[0])
    thin = training.thinned(full, 7)
    assert len(thin.kinds) == int(np.ceil(len(full.kinds) / 7))
    assert set(np.unique(thin.kinds)) == {SONG, SPEECH, MIXED}
    assert training.thinned(full, 1) is full
    samples, read = trainer._samples()
    assert len(samples) == 2 and read == trainer.sources  # two services fit whole
    assert len(samples[0].kinds) == len(full.kinds)
