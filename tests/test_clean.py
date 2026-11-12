import pytest

from livetranslate.translate.clean import blocked_sequences, clean, has_swearing


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        ("I was fucking crazy.", "en", "I was crazy."),
        ("I was out of my fucking mind.", "en", "I was out of my mind."),
        ("What the fuck is this?", "en", "What on earth is this?"),
        ("Fucking hell, it's true!", "en", "Hell, it's true!"),
        ("Goddamn it, that's true!", "en", "Oh no, that's true!"),
        ("This is bullshit.", "en", "This is nonsense."),
        ("He's an asshole.", "en", "He's a fool."),
        ("Damn, what a day.", "en", "What a day."),
        ("They are bastards.", "en", "They are fools."),
        ("Я был, блядь, в шоке.", "ru", "Я был в шоке."),
        ("Это полная хуйня.", "ru", "Это полная ерунда."),
        ("Он нас заебал.", "ru", "Он нас."),
        ("Це, бляха, правда.", "uk", "Це правда."),
        ("Dat is echt kut.", "nl", "Dat is echt."),
    ],
)
def test_clean(text, lang, expected):
    assert clean(text, lang) == expected


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Without faith the damned go to hell, and damnation awaits.", "en"),
        ("Jesus rode into Jerusalem on an ass.", "en"),
        ("Не судите, да не судимы будете. Что ты смотришь на сучок в глазе брата твоего?", "ru"),
        ("Хлеба насущного дай нам на сей день. Небеса и херувимы. Употребляя, колебался.", "ru"),
        ("Хліб наш насущний дай нам сьогодні. Зебра і лайнер онлайн.", "uk"),
        ("Hebreeën, de eikel en de kanker.", "nl"),
        ("Der wird verdammt werden.", "de"),
        ("La hostia consagrada, un juicio putativo.", "es"),
    ],
)
def test_church_words_stay(text, lang):
    assert not has_swearing(text, lang)
    assert clean(text, lang) == text


def test_unknown_language_is_left_alone():
    assert clean("whatever", "xx") == "whatever"


def test_blocked_sequences():
    vocab = {"▁fucking": 1, "▁fuck": 2, "▁luck": 3, "▁damned": 4, "▁shitake": 5, "uck": 6}
    pieces = {"fuck": ["▁f", "uck"], "x fuck": ["▁x", "▁f", "uck"]}
    blocked = blocked_sequences("en", vocab, lambda t: pieces.get(t, ["▁" + t]))
    assert ["▁fucking"] in blocked and ["▁fuck"] in blocked and ["▁f", "uck"] in blocked
    assert ["▁luck"] not in blocked and ["▁damned"] not in blocked and ["uck"] not in blocked
    assert ["▁damn"] not in blocked  # only an exact word: blocking it would also block "damnation"


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [  # what NLLB still wrote with the words blocked
        ("God damn it, it is true!", "en", "Oh no, it is true!"),
        ("This is just shit.", "en", "This is just rubbish."),
        ("Це просто дерьмо.", "uk", "Це просто."),
    ],
)
def test_clean_after_nllb(text, lang, expected):
    assert clean(text, lang) == expected
