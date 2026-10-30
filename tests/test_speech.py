import numpy as np

from livetranslate.tts.piper import MAX_BACKLOG_S, STREAM_RATE, LanguageStream


def seconds(n: float, value: int) -> np.ndarray:
    return np.full(int(n * STREAM_RATE), value, dtype=np.int16)


def test_backlog_skips_the_oldest_waiting_sentences():
    stream = LanguageStream("ru", bank=None)
    for i in range(1, 5):
        stream._add(seconds(3, i))
    # The first sentence may already be playing, so it stays; 2 and 3 are skipped.
    assert [int(s[0]) for s in stream._pcm] == [1, 4]
    assert stream.backlog_seconds <= MAX_BACKLOG_S


def test_a_long_sentence_is_never_dropped():
    stream = LanguageStream("ru", bank=None)
    stream._add(seconds(12, 1))
    stream._add(seconds(12, 2))
    assert [int(s[0]) for s in stream._pcm] == [1, 2]
