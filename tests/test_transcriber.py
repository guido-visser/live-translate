import numpy as np

from livetranslate.config import AsrSettings
from livetranslate.stt.transcriber import SAMPLE_RATE, HypothesisBuffer, StreamingTranscriber, Word, join_words

SCRIPT = [
    Word(0.2, 0.7, " Goedemorgen"),
    Word(0.8, 1.3, " allemaal."),
    Word(1.5, 1.9, " Welkom"),
    Word(2.0, 2.2, " in"),
    Word(2.3, 2.5, " de"),
    Word(2.6, 3.1, " kerk"),
    # pause from 3.1 to 4.5
    Word(4.5, 5.0, " We"),
    Word(5.1, 5.5, " gaan"),
    Word(5.6, 6.1, " zingen."),
]
SPEECH_END = 6.1
TOTAL = 8.0


def make_audio() -> np.ndarray:
    audio = np.zeros(int(TOTAL * SAMPLE_RATE), dtype=np.float32)
    for w in SCRIPT:
        audio[int(w.start * SAMPLE_RATE) : int(w.end * SAMPLE_RATE)] = 0.1
    # Fill short gaps between words inside a phrase so "speech" is continuous.
    audio[int(0.2 * SAMPLE_RATE) : int(3.1 * SAMPLE_RATE)] = 0.1
    audio[int(4.5 * SAMPLE_RATE) : int(SPEECH_END * SAMPLE_RATE)] = 0.1
    return audio


def run(settings: AsrSettings) -> tuple[list[str], list[str]]:
    sentences: list[str] = []
    partials: list[str] = []
    holder: dict = {}

    def transcribe(audio: np.ndarray, prompt: str) -> list[Word]:
        t = holder["t"]
        end = t.offset + len(audio) / SAMPLE_RATE
        # A real model only "hears" words that are complete within the buffer.
        return [
            Word(w.start - t.offset, w.end - t.offset, w.text)
            for w in SCRIPT
            if w.start >= t.offset - 0.05 and w.end <= end
        ]

    def has_speech(audio: np.ndarray) -> bool:
        return bool(np.abs(audio).max(initial=0) > 0.01)

    t = StreamingTranscriber(transcribe, has_speech, partials.append, sentences.append, settings)
    holder["t"] = t
    audio = make_audio()
    step = int(0.25 * SAMPLE_RATE)
    for i in range(0, len(audio), step):
        t.add_audio(audio[i : i + step])
        if t.ready():
            t.process()
    return sentences, partials


def test_sentences_come_out_in_order_without_duplicates():
    sentences, partials = run(AsrSettings(min_chunk_s=0.5, silence_commit_s=0.6))
    assert sentences == ["Goedemorgen allemaal.", "Welkom in de kerk", "We gaan zingen."]
    assert partials[-1] == ""


def test_pause_finalizes_unpunctuated_sentence():
    sentences, _ = run(AsrSettings(min_chunk_s=0.5, silence_commit_s=0.6))
    assert "Welkom in de kerk" in sentences


def test_long_sentence_is_forced_out():
    sentences, _ = run(AsrSettings(min_chunk_s=0.5, silence_commit_s=5.0, max_sentence_s=1.5))
    assert " ".join(sentences).split() == join_words(SCRIPT).split()
    assert len(sentences) >= 3


def test_local_agreement_commits_common_prefix():
    hyp = HypothesisBuffer()
    hyp.insert([Word(0, 0.5, " een"), Word(0.6, 1.0, " twee")], 0)
    assert hyp.flush() == []
    hyp.insert([Word(0, 0.5, " een"), Word(0.6, 1.0, " drie"), Word(1.1, 1.5, " vier")], 0)
    committed = hyp.flush()
    assert [w.text for w in committed] == [" een"]
    assert [w.text for w in hyp.buffer] == [" drie", " vier"]


def test_reset_discards_open_text():
    out: list[str] = []
    t = StreamingTranscriber(
        lambda a, p: [Word(0.0, 0.5, " hallo")], lambda a: True, lambda p: None, out.append, AsrSettings()
    )
    t.add_audio(np.full(SAMPLE_RATE, 0.1, dtype=np.float32))
    t.process()
    t.reset()
    assert t.pending == [] and t.hyp.buffer == [] and out == []


def test_finalize_emits_open_text():
    out: list[str] = []
    t = StreamingTranscriber(
        lambda a, p: [Word(0.0, 0.4, " Dank"), Word(0.5, 0.9, " u")],
        lambda a: True,
        lambda p: None,
        out.append,
        AsrSettings(),
    )
    t.add_audio(np.full(SAMPLE_RATE, 0.1, dtype=np.float32))
    t.process()  # first pass: nothing agreed yet
    assert out == []
    t.finalize()
    assert out == ["Dank u"]
