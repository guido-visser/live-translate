"""Teaches the song detection this church's sound, from recordings of its services.

Nobody labels anything. YAMNet's scores are clear about most of a service, so the long and clear
stretches of song and speech become the examples, and everything doubtful is left out. A model
that has only seen those learns "a band means a song" and would skip a prayer over the piano, so
it also gets this church's own speech mixed over its own music, as speech.

What is kept per service is a small sample of YAMNet's description of the sound, never the audio.
A new model only takes over when it does at least as well as the rules on services it hasn't seen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import numpy as np

from .config import SongModel
from .stt.sound import ON_AFTER, SONG_LEVELS, TRAINED_LIMIT, SingingGate, SoundClassifier, SoundScores, song_chance

log = logging.getLogger(__name__)

RATE = 16000
PASS_S = 1  # the app judges the last two seconds, once about every second
WINDOW_S = 2

# What counts as clear, as averages over half a minute.
SMOOTH_S = 15
SONG_MIN_MUSIC, SONG_MAX_SPEECH, SONG_MAX_VOICED = 0.6, 0.1, 0.3
SPEECH_MIN_SPEECH, SPEECH_MIN_VOICED, SPEECH_MAX_MUSIC = 0.5, 0.4, 0.05
MIN_STRETCH_S = 60
EDGE_S = 10  # a switch between song and speech is never clear
MIN_EACH_S = 180  # a service needs this much of both to learn from

RUN_S = 30  # samples are unbroken runs, so a test can follow them the way the app does
RUNS_PER_KIND = 20
MIXES = (12, 6, 0, -6)  # speech against the music, in dB
RUNS_PER_MIX = 4

SPEECH, SONG, MIXED = 0, 1, 2
DIMENSIONS = 128
MIX_WEIGHT = 0.25  # compared with 1 for song and 1 for speech
MAX_TEST_SERVICES = 5
MAX_LEARN_PASSES = 60000  # about 250 MB in memory; with more services each gives a little less
MIN_SERVICES = 2  # one to learn from and one to test on

# A new model must keep to these, in % wrong on services it hasn't seen.
MAX_SPEECH_SKIPPED = 1.0
SONG_LEEWAY = 2.0

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID = re.compile(r"^[\w-]{6,}$")
MIN_LISTED_S = 60 * 60  # in a channel, what is shorter is rarely a whole service: a sermon has no songs


class Stopped(Exception):
    pass


# -- choosing the examples -------------------------------------------------------------------------


def _average(values: np.ndarray, reach: int = SMOOTH_S) -> np.ndarray:
    total = np.r_[0.0, np.cumsum(values, dtype=np.float64)]
    ends = np.minimum(np.arange(len(values)) + reach + 1, len(values))
    starts = np.maximum(np.arange(len(values)) - reach, 0)
    return (total[ends] - total[starts]) / (ends - starts)


def _stretches(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return [(int(a) + EDGE_S, int(b) - EDGE_S) for a, b in zip(edges[::2], edges[1::2]) if b - a >= MIN_STRETCH_S]


def clear_stretches(rows: np.ndarray) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Stretches, in passes, that are clearly a song and clearly speech. rows: speech, singing, music, voiced."""
    speech, music, voiced = _average(rows[:, 0]), _average(rows[:, 2]), _average(rows[:, 3])
    songs = (music > SONG_MIN_MUSIC) & (speech < SONG_MAX_SPEECH) & (voiced < SONG_MAX_VOICED)
    talks = (speech > SPEECH_MIN_SPEECH) & (voiced > SPEECH_MIN_VOICED) & (music < SPEECH_MAX_MUSIC)
    return _stretches(songs), _stretches(talks)


def _run_starts(stretches: list[tuple[int, int]], count: int) -> list[int]:
    """Up to this many runs, evenly spread over the stretches."""
    places = [start for a, b in stretches for start in range(a, b - RUN_S + 1, RUN_S)]
    if len(places) <= count:
        return places
    return [places[i] for i in np.linspace(0, len(places) - 1, count).astype(int)]


# -- learning --------------------------------------------------------------------------------------


@dataclass
class Sample:
    """One service, as far as training is concerned."""

    descriptions: np.ndarray  # float32, per pass
    rows: np.ndarray  # speech, singing, music, voiced, per pass
    kinds: np.ndarray  # SPEECH, SONG or MIXED, per pass
    runs: np.ndarray  # unbroken stretches have the same number
    mixes: np.ndarray | None = None  # dB per pass; only for MIXED, otherwise 0


def fit(descriptions: np.ndarray, is_song: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, float]:
    """A model as one number per description value and a bias: nearly nothing to run next to a service.

    Logistic regression. Most of the 1024 values say much the same, so it learns in the strongest
    directions only, which keeps a few services from being learned by heart.
    """
    mean = descriptions.mean(axis=0)
    centered = descriptions - mean
    strengths, directions = np.linalg.eigh((centered.T @ centered / len(centered)).astype(np.float64))
    dims = min(DIMENSIONS, descriptions.shape[1])
    directions = directions[:, -dims:]
    scale = np.sqrt(np.maximum(strengths[-dims:], 1e-9))
    inputs = np.c_[centered @ directions / scale, np.ones(len(centered))]
    # As many examples, however many services: otherwise more services would mean less caution.
    weights = weights * 1000 / weights.sum()
    caution = np.eye(dims + 1)
    caution[-1, -1] = 0
    b = np.zeros(dims + 1)
    for _ in range(25):
        chance = np.clip(1 / (1 + np.exp(-np.clip(inputs @ b, -30, 30))), 1e-6, 1 - 1e-6)
        pull = weights * chance * (1 - chance)
        step = np.linalg.solve((inputs * pull[:, None]).T @ inputs + caution, inputs.T @ (weights * (chance - is_song)) + caution @ b)
        b -= step
        if np.abs(step).max() < 1e-4:
            break
    result = directions @ (b[:-1] / scale)
    return result, float(b[-1] - mean.astype(np.float64) @ result)


def learn(samples: list[Sample]) -> tuple[np.ndarray, float]:
    """Songs, speech and mixed speech all count the same per kind, not per second."""
    descriptions = np.concatenate([s.descriptions for s in samples])
    kinds = np.concatenate([s.kinds for s in samples])
    weights = np.zeros(len(kinds))
    for kind, weight in ((SPEECH, 1.0), (SONG, 1.0), (MIXED, MIX_WEIGHT)):
        chosen = kinds == kind
        if chosen.any():
            weights[chosen] = weight / chosen.sum()
    return fit(descriptions, (kinds == SONG).astype(np.float64), weights)


def _follow(judge: Callable[[np.ndarray, np.ndarray], list[bool]], sample: Sample) -> np.ndarray:
    """The app's decision on every pass, run by run."""
    decisions = np.zeros(len(sample.kinds), dtype=bool)
    for run in np.unique(sample.runs):
        chosen = sample.runs == run
        decisions[chosen] = judge(sample.rows[chosen], sample.descriptions[chosen])
    return decisions


def by_rules(level: int) -> Callable:
    def judge(rows: np.ndarray, _descriptions: np.ndarray) -> list[bool]:
        gate = SingingGate(SONG_LEVELS[level])
        return [gate.update(SoundScores(float(r[0]), float(r[1]), float(r[2])), float(r[3])) for r in rows]

    return judge


def by_model(weights: np.ndarray, bias: float) -> Callable:
    def judge(_rows: np.ndarray, descriptions: np.ndarray) -> list[bool]:
        gate = SingingGate()
        return [gate.decide(song_chance(weights, bias, description) >= TRAINED_LIMIT) for description in descriptions]

    return judge


def score(judge: Callable, samples: list[Sample]) -> dict[str, float]:
    """% wrong: song let through, clear speech skipped, and speech over music skipped."""
    wrong = {"song": [0, 0], "speech": [0, 0], "mixed": [0, 0]}
    for sample in samples:
        decisions = _follow(judge, sample)
        # Every run starts with the decision still to be made, which says nothing about the judge.
        settled = np.r_[np.zeros(1, dtype=bool), sample.runs[1:] == sample.runs[:-1]]
        for _ in range(ON_AFTER):
            settled = np.r_[False, settled[:-1] & settled[1:]]
        for key, kind, mistakes in (
            ("song", SONG, ~decisions),
            ("speech", SPEECH, decisions),
            ("mixed", MIXED, decisions),
        ):
            chosen = (sample.kinds == kind) & settled
            wrong[key][0] += int(mistakes[chosen].sum())
            wrong[key][1] += int(chosen.sum())
    return {key: round(100 * a / max(1, b), 1) for key, (a, b) in wrong.items()}


def thinned(sample: Sample, every: int) -> Sample:
    if every <= 1:
        return sample
    return Sample(sample.descriptions[::every], sample.rows[::every], sample.kinds[::every], sample.runs[::every], sample.mixes[::every])


def compare(samples: list[Sample], level: int, whole: Callable[[int], Sample] | None = None) -> dict:
    """Test on services kept out of the learning, against the rules, on the very same audio.

    samples may be thinned to save memory; whole gives a service complete, for the test.
    """
    test_on = sorted(set(np.linspace(0, len(samples) - 1, min(MAX_TEST_SERVICES, len(samples))).astype(int)))
    totals: dict[str, dict[str, list[float]]] = {"rules": {}, "trained": {}}
    for i in test_on:
        rest = [s for j, s in enumerate(samples) if j != i]
        weights, bias = learn(rest)
        for name, judge in (("rules", by_rules(level)), ("trained", by_model(weights, bias))):
            for key, value in score(judge, [whole(i) if whole else samples[i]]).items():
                totals[name].setdefault(key, []).append(value)
    return {name: {key: round(float(np.mean(v)), 1) for key, v in values.items()} for name, values in totals.items()}


def verdict(comparison: dict) -> str:
    """Why the new model can't take over; empty when it can."""
    rules, trained = comparison["rules"], comparison["trained"]
    if trained["speech"] > max(MAX_SPEECH_SKIPPED, rules["speech"]):
        return "speech"
    if trained["song"] > rules["song"] + SONG_LEEWAY:
        return "song"
    if trained["mixed"] > rules["mixed"]:
        return "mixed"
    return ""


# -- reading a recording ---------------------------------------------------------------------------


def describe(audio: np.ndarray, classifier: SoundClassifier, voiced, tick: Callable[[float], None]) -> tuple[np.ndarray, np.ndarray]:
    rows, descriptions = [], []
    last = len(audio) // RATE - WINDOW_S
    for second in range(0, last + 1, PASS_S):
        window = audio[second * RATE : (second + WINDOW_S) * RATE]
        scores, description = classifier.analyse(window)
        rows.append((scores.speech, scores.singing, scores.music, voiced(window)))
        descriptions.append(description)
        if second % 30 == 0:
            tick(second / max(1, last))
    return np.array(rows, dtype=np.float32).reshape(-1, 4), np.array(descriptions, dtype=np.float32).reshape(-1, 1024)


def mix_runs(audio, talks, songs, classifier, voiced, rows, rng, check=lambda: None):
    """Speech with a song under it, at several strengths: what a prayer over the band sounds like."""
    long_talks = [s for s in talks if s[1] - s[0] >= RUN_S + WINDOW_S]
    long_songs = [s for s in songs if s[1] - s[0] >= RUN_S + WINDOW_S]
    out = []
    length = RUN_S + WINDOW_S
    for db in MIXES:
        for _ in range(RUNS_PER_MIX):
            check()
            a, b = long_talks[rng.integers(len(long_talks))]
            start = int(rng.integers(a, b - length + 1))
            for _try in range(20):  # someone really talking the whole time, when there is such a place
                if rows[start : start + RUN_S, 3].mean() >= 0.5:
                    break
                a, b = long_talks[rng.integers(len(long_talks))]
                start = int(rng.integers(a, b - length + 1))
            c, d = long_songs[rng.integers(len(long_songs))]
            under = int(rng.integers(c, d - length + 1))
            talk = audio[start * RATE : (start + length) * RATE]
            music = audio[under * RATE : (under + length) * RATE]
            loudness = np.sqrt((talk**2).mean() / max((music**2).mean(), 1e-9))
            mixed = talk + loudness * 10 ** (-db / 20) * music
            mixed = mixed / max(1.0, float(np.abs(mixed).max()))
            run_rows, run_descriptions = [], []
            for second in range(RUN_S):
                window = mixed[second * RATE : (second + WINDOW_S) * RATE]
                scores, description = classifier.analyse(window)
                run_rows.append((scores.speech, scores.singing, scores.music, voiced(window)))
                run_descriptions.append(description)
            out.append((db, np.array(run_rows, dtype=np.float32), np.array(run_descriptions, dtype=np.float32)))
    return out


def sample_service(audio, classifier, voiced, seed, tick=lambda share: None, check=lambda: None) -> tuple[Sample | None, int, int]:
    """What to learn from in one recording, and how many seconds of song and speech are clear in it."""

    def describing(share: float) -> None:
        check()
        tick(share * 0.85)

    rows, descriptions = describe(audio, classifier, voiced, describing)
    songs, talks = clear_stretches(rows)
    song_s, speech_s = sum(b - a for a, b in songs), sum(b - a for a, b in talks)
    if song_s < MIN_EACH_S or speech_s < MIN_EACH_S:
        return None, song_s, speech_s
    parts = []
    run = 0
    for kind, stretches in ((SONG, songs), (SPEECH, talks)):
        for start in _run_starts(stretches, RUNS_PER_KIND):
            parts.append((kind, 0, rows[start : start + RUN_S], descriptions[start : start + RUN_S], run))
            run += 1
    tick(0.85)
    for db, mixed_rows, mixed_descriptions in mix_runs(audio, talks, songs, classifier, voiced, rows, np.random.default_rng(seed), check):
        parts.append((MIXED, db, mixed_rows, mixed_descriptions, run))
        run += 1
    return (
        Sample(
            descriptions=np.concatenate([p[3] for p in parts]),
            rows=np.concatenate([p[2] for p in parts]),
            kinds=np.concatenate([np.full(len(p[2]), p[0], dtype=np.int8) for p in parts]),
            runs=np.concatenate([np.full(len(p[2]), p[4], dtype=np.int32) for p in parts]),
            mixes=np.concatenate([np.full(len(p[2]), p[1], dtype=np.int8) for p in parts]),
        ),
        song_s,
        speech_s,
    )


# -- what people can add ---------------------------------------------------------------------------


def youtube_video_id(text: str) -> str | None:
    """The video in a link; None for a channel, a playlist or anything else."""
    url = urlparse(text)
    host = url.netloc.lower()
    parts = [p for p in url.path.split("/") if p]
    found = None
    if host == "youtu.be" and parts:
        found = parts[0]
    elif host in YOUTUBE_HOSTS:
        if parts[:1] == ["watch"]:
            found = (parse_qs(url.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("live", "shorts", "embed", "v"):
            found = parts[1]
    return found if found and VIDEO_ID.match(found) else None


def is_youtube(text: str) -> bool:
    url = urlparse(text)
    return url.scheme in ("http", "https") and url.netloc.lower() in YOUTUBE_HOSTS


class _Quiet:
    """yt-dlp writes to the console otherwise, which the app doesn't have."""

    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        log.info("yt-dlp: %s", message)


def list_youtube(url: str) -> list[dict]:
    """The videos of a channel or playlist: id, title and length, without downloading anything."""
    from yt_dlp import YoutubeDL

    options = {"quiet": True, "no_warnings": True, "noprogress": True, "extract_flat": "in_playlist", "logger": _Quiet(), "ignoreerrors": True}
    found: dict[str, dict] = {}

    def visit(entry: dict | None, depth: int = 0) -> None:
        if not entry or depth > 3:
            return
        if entry.get("entries") is not None:
            for child in entry["entries"]:
                visit(child, depth + 1)
            return
        link = entry.get("url") or entry.get("webpage_url") or ""
        video = youtube_video_id(link) or (entry.get("id") if entry.get("ie_key") == "Youtube" else None)
        if video:
            if "/shorts/" not in link:
                found.setdefault(
                    video,
                    {
                        "source": f"https://www.youtube.com/watch?v={video}",
                        "title": entry.get("title") or video,
                        "duration": entry.get("duration"),
                    },
                )
        elif link and entry.get("_type") == "url" and not link.rstrip("/").endswith("/shorts"):
            with YoutubeDL(options) as nested:  # a channel lists its tabs: videos and live streams
                visit(nested.extract_info(link, download=False), depth + 1)

    with YoutubeDL(options) as ydl:
        visit(ydl.extract_info(url, download=False))
    videos = list(found.values())
    for video in videos:
        video["suggested"] = video["duration"] is None or video["duration"] >= MIN_LISTED_S
    return videos


def download_youtube(url: str, folder: Path, tick: Callable[[float], None], check: Callable[[], None]) -> tuple[Path, str]:
    """Only the sound; returns the file and the title."""
    from yt_dlp import YoutubeDL

    def progress(state: dict) -> None:
        check()
        total = state.get("total_bytes") or state.get("total_bytes_estimate")
        if total:
            tick(min(1.0, state.get("downloaded_bytes", 0) / total))

    folder.mkdir(parents=True, exist_ok=True)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(folder / "audio.%(ext)s"),
        "logger": _Quiet(),
        "progress_hooks": [progress],
        "retries": 3,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)), info.get("title") or url


def _message(error: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(error)).removeprefix("ERROR: ").strip()[:300]


# -- the work behind Settings → Training -----------------------------------------------------------


class Trainer:
    def __init__(self, data: Path, get_settings, may_run: Callable[[], bool], use_model: Callable[[SongModel], None], gpu=lambda: True) -> None:
        self.folder = data / "training"
        self.get_settings, self.may_run, self.use_model, self.gpu = get_settings, may_run, use_model, gpu
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"  # idle | working | learning
        self.stopped_for_service = False
        self.current: str | None = None
        self.step = ""  # downloading | reading
        self.share = 0.0
        self.error = ""
        self.sources: list[dict] = []
        self.result: dict | None = None
        self._load()

    # -- what is on the list

    @property
    def _list_file(self) -> Path:
        return self.folder / "sources.json"

    @property
    def _result_file(self) -> Path:
        return self.folder / "result.json"

    def _load(self) -> None:
        try:
            self.sources = json.loads(self._list_file.read_text("utf-8"))
            for source in self.sources:
                if source["state"] == "busy":  # the app was closed during it
                    source["state"] = "waiting"
        except (OSError, ValueError):
            self.sources = []
        try:
            self.result = json.loads(self._result_file.read_text("utf-8"))
        except (OSError, ValueError):
            self.result = None
        shutil.rmtree(self.folder / "downloads", ignore_errors=True)

    def _save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            temporary = self._list_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.sources, ensure_ascii=False, indent=1), "utf-8")
            temporary.replace(self._list_file)

    def _sample_file(self, source_id: str) -> Path:
        return self.folder / f"{source_id}.npz"

    def add(self, text: str) -> dict:
        """Lines with links or file paths. Channels and playlists come back, to choose from."""
        added, channels, rejected = 0, [], []
        for line in text.splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            if is_youtube(line):
                video = youtube_video_id(line)
                if video:
                    added += self._add({"id": f"yt-{video}", "kind": "youtube", "source": f"https://www.youtube.com/watch?v={video}", "title": line})
                else:
                    channels.append(line)
            elif Path(line).is_file():
                added += self._add_file(line)
            else:
                rejected.append(line)
        return {"added": added, "channels": channels, "rejected": rejected}

    def add_listed(self, videos: list[dict]) -> int:
        added = 0
        for video in videos:
            found = youtube_video_id(str(video.get("source", "")))
            if found:
                added += self._add({"id": f"yt-{found}", "kind": "youtube", "source": f"https://www.youtube.com/watch?v={found}", "title": str(video.get("title") or found)[:200]})
        return added

    def _add_file(self, path: str) -> bool:
        resolved = str(Path(path).resolve())
        digest = hashlib.sha1(resolved.lower().encode()).hexdigest()[:16]
        return self._add({"id": f"file-{digest}", "kind": "file", "source": resolved, "title": Path(resolved).name})

    def _add(self, source: dict) -> bool:
        with self._lock:
            if any(s["id"] == source["id"] for s in self.sources):
                return False
            self.sources.append({**source, "state": "waiting", "detail": "", "song_s": 0, "speech_s": 0})
        self._save()
        return True

    def remove(self, source_id: str) -> None:
        with self._lock:
            if self.current == source_id:  # being read right now: the Stop button is for that
                return
            self.sources = [s for s in self.sources if s["id"] != source_id]
        self._sample_file(source_id).unlink(missing_ok=True)
        self._save()

    # -- status

    def status(self) -> dict:
        with self._lock:
            done = sum(s["state"] == "done" for s in self.sources)
            return {
                "state": self.state,
                "may_run": self.may_run(),
                "stopped_for_service": self.stopped_for_service,
                "current": self.current,
                "step": self.step,
                "share": round(self.share, 3),
                "error": self.error,
                "sources": [dict(s) for s in self.sources],
                "usable": done,
                "needed": MIN_SERVICES,
                "result": self.result,
            }

    # -- working

    def start(self) -> bool:
        with self._lock:
            if self.state != "idle" or not self.may_run():
                return False
            self.state = "working"
            self.stopped_for_service = False
            self.error = ""
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="training", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _check(self) -> None:
        if self._stop.is_set():
            raise Stopped
        if not self.may_run():  # a service comes first; what is finished stays finished
            self.stopped_for_service = True
            raise Stopped

    def _run(self) -> None:
        classifier = None
        try:
            for source in [s for s in self.sources if s["state"] in ("waiting", "error")]:
                self._check()
                if classifier is None:
                    classifier = SoundClassifier(self.gpu())
                if any(s["id"] == source["id"] for s in self.sources):
                    self._read_source(source, classifier)
            classifier = None
            self.current, self.step = None, ""
            self._check()
            self._learn()
        except Stopped:
            log.info("Training stopped")
        except Exception as exc:
            log.exception("Training failed")
            self.error = _message(exc)
        finally:
            with self._lock:
                self.state, self.current, self.step, self.share = "idle", None, "", 0.0
            self._save()

    def _read_source(self, source: dict, classifier: SoundClassifier) -> None:
        from faster_whisper import decode_audio

        from .stt.transcriber import voiced_share

        self.current = source["id"]
        source.update(state="busy", detail="")
        downloads = self.folder / "downloads" / source["id"]
        try:
            if source["kind"] == "youtube":
                self.step, self.share = "downloading", 0.0
                path, title = download_youtube(source["source"], downloads, lambda share: setattr(self, "share", share), self._check)
                source["title"] = title
                self._save()
            else:
                path = Path(source["source"])
            self.step, self.share = "reading", 0.0
            audio = decode_audio(str(path), sampling_rate=RATE)
            sample, song_s, speech_s = sample_service(
                audio, classifier, voiced_share, int(hashlib.sha1(source["id"].encode()).hexdigest()[:8], 16),
                lambda share: setattr(self, "share", share), self._check,
            )
            source.update(song_s=song_s, speech_s=speech_s)
            if sample is None:
                source.update(state="skipped", detail="unclear")
                return
            np.savez_compressed(
                self._sample_file(source["id"]),
                descriptions=sample.descriptions, rows=sample.rows, kinds=sample.kinds, runs=sample.runs, mixes=sample.mixes,
            )
            source["state"] = "done"
        except Stopped:
            source["state"] = "waiting"
            raise
        except Exception as exc:
            log.warning("Couldn't read %s: %s", source["source"], exc)
            source.update(state="error", detail=_message(exc))
        finally:
            shutil.rmtree(downloads, ignore_errors=True)
            self._save()

    def _read_sample(self, source: dict) -> Sample:
        with np.load(self._sample_file(source["id"])) as f:
            return Sample(f["descriptions"], f["rows"], f["kinds"], f["runs"], f["mixes"])

    def _samples(self) -> tuple[list[Sample], list[dict]]:
        """All services, thinned so that hundreds of them still fit in memory."""
        ready = [s for s in self.sources if s["state"] == "done"]
        passes = 2 * RUNS_PER_KIND * RUN_S + len(MIXES) * RUNS_PER_MIX * RUN_S
        every = int(np.ceil(len(ready) * passes / MAX_LEARN_PASSES))
        samples, read = [], []
        for source in ready:
            try:
                samples.append(thinned(self._read_sample(source), every))
                read.append(source)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Couldn't read what was kept of %s: %s", source["source"], exc)
                source.update(state="waiting", detail="")
        return samples, read

    def _learn(self) -> None:
        samples, read = self._samples()
        if len(samples) < MIN_SERVICES:
            return
        self.state = "learning"
        comparison = compare(samples, self.get_settings().song_level, lambda i: self._read_sample(read[i]))
        problem = verdict(comparison)
        result = {**comparison, "services": len(samples), "used": not problem, "problem": problem, "at": time.strftime("%Y-%m-%d %H:%M")}
        if not problem:
            weights, bias = learn(samples)
            self.use_model(
                SongModel(
                    weights=[round(float(w), 5) for w in weights],
                    bias=round(bias, 5),
                    services=len(samples),
                    trained_at=time.strftime("%Y-%m-%d"),
                )
            )
        self.result = result
        self.folder.mkdir(parents=True, exist_ok=True)
        self._result_file.write_text(json.dumps(result), "utf-8")
        log.info("Training on %d services: %s", len(samples), result)
