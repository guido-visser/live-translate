"""Keep swear words out of translations, for a church service.

The engines sometimes swear where the speaker didn't: NLLB turned "Ik was verdomme gek"
into "I was fucking crazy". Two layers stop that:

* NLLB: the words are blocked while it translates (`suppress_sequences`), so it picks
  another word itself: "I was crazy", "What the hell is this?". That reads naturally.
* Every engine (LibreTranslate has no such option): a word filter afterwards. It drops
  swear words used for emphasis and swaps a few for a mild word. Clumsier, but a net.

Only clear swear words are listed. Words with a normal or Bible meaning stay allowed,
however they're used: hell, damned, ass, whore, bloody, "сук" (branch), "хрен" (horseradish),
"херувим", "hostia" (the host), "Hure", "kanker". Russian roots are matched with care:
"еба" is also in "хлеба", "бля" in "употребляя".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache


@dataclass(frozen=True)
class WordList:
    stems: tuple[str, ...] = ()  # a word starting with one of these, any ending ("fuck" → "fucking")
    words: tuple[str, ...] = ()  # only these exact words; filtered afterwards, NLLB doesn't block them
    prefixes: tuple[str, ...] = ()  # verb prefixes that may come before a stem ("за" + "еб…")
    # Swapped instead of dropped, checked first: (regex, replacement).
    swaps: tuple[tuple[str, str], ...] = field(default=())


RU_PREFIXES = ("за", "вы", "на", "по", "от", "отъ", "у", "раз", "разъ", "рас", "съ", "подъ", "до", "пере", "при",
               "объ", "въ", "изъ", "о", "недо")
# No plain "з": "з" + "еб…" would be "зебра".
UK_PREFIXES = ("за", "ви", "на", "по", "від", "у", "роз", "до", "пере", "при", "об", "від'", "з'", "роз'")

INSULTS = "(?:motherfucker|asshole|arsehole|bastard|dickhead|cunt|twat|bitch)"

LISTS: dict[str, WordList] = {
    "en": WordList(
        stems=("fuck", "motherfuck", "effing", "shit", "bullshit", "horseshit", "asshole", "arsehole", "bitch",
               "cunt", "dickhead", "goddamn", "goddam", "dammit", "damnit", "crap", "piss", "bastard", "slut", "twat",
               "wtf"),
        words=("damn",),  # not "damned", "damnation"
        swaps=(
            (r"what the fuck", "what on earth"),
            (r"(where|who|why|how) the fuck", r" on earth"),
            (r"fuck(?:ed|ing)? (?:it )?up", "messed up"),
            (r"fuck(?:s|ing)? off", "go away"),
            (r"piss(?:ed|es|ing)? off", "angry"),
            (r"pissed", "angry"),
            (r"holy shit", "wow"),
            (r"(?:god ?)?damn it|dammit|damnit", "oh no"),
            (r"bullshit|horseshit|crap", "nonsense"),
            (r"shitty|crappy", "bad"),
            (r"shit", "rubbish"),
            (r"fucked", "ruined"),
            (rf"an? {INSULTS}", "a fool"),
            (rf"{INSULTS}e?s", "fools"),
            (INSULTS, "fool"),
        ),
    ),
    "nl": WordList(
        stems=("fuck", "fokking", "godverdomme", "godverdorie", "verdomme", "kut", "klote", "klootzak", "kolere",
               "shit", "kankerlijer"),
        words=("lul",),
        swaps=((r"(?:god)?verdomme", "jeetje"),),
    ),
    "de": WordList(stems=("scheiß", "scheiss", "fick", "arschloch", "wichser", "fotze", "fuck")),
    "fr": WordList(
        stems=("putain", "merde", "connard", "connasse", "salope", "enculé", "encule", "foutre", "fuck"),
        words=("bordel", "con", "conne", "chier"),
    ),
    "es": WordList(
        stems=("mierd", "joder", "jodid", "gilipoll", "cabrón", "cabron", "coño", "fuck"),
        words=("puta", "putas", "puto", "putos"),  # not "putativo"
    ),
    "pt": WordList(
        stems=("porra", "caralh", "merda", "foder", "fodid", "foda", "cacete", "fuck"),
        words=("puta", "putas", "puto", "putos"),
    ),
    "it": WordList(stems=("cazz", "vaffancul", "stronz", "minchi", "merd", "puttan", "fottut", "fottere", "fuck")),
    "pl": WordList(
        stems=("kurw", "pierdol", "pierdal", "jeb", "chuj", "huj", "skurwysyn", "fuck"),
        prefixes=("za", "wy", "s", "od", "do", "prze", "przy", "na", "po", "roz", "u"),
    ),
    "ru": WordList(
        stems=("бля", "хуй", "хуе", "хуё", "хуя", "хуи", "пизд", "ёб", "еб", "мудак", "мудил", "дерьм", "говн",
               "шлюх", "ублюд", "сучар", "херн", "нахер", "похер", "нахрен", "похрен", "fuck"),
        # Only in the after-filter: "сука" is also "of the branch", "хер" starts "херувим".
        words=("сука", "суки", "сукой", "хер"),
        prefixes=RU_PREFIXES,
        swaps=((r"дерьмо|херня|хуйня", "ерунда"), (r"мудак|ублюдок", "дурак")),
    ),
    "uk": WordList(
        stems=("бля", "хуй", "хує", "хуї", "хуя", "пизд", "їб", "єб", "ёб", "еб", "мудак", "мудил", "гівн",
               "дерьм", "говн", "шльондр", "сучар", "курв", "срак", "срань", "херн", "нахер", "похер", "нахрін", "похрін", "fuck"),
        words=("сука", "суки", "хер", "лайно", "лайна", "лайном", "лайні"),  # not "лайнер", "онлайн"
        prefixes=UK_PREFIXES + RU_PREFIXES,
        swaps=((r"лайно|херня|хуйня", "дурниця"), (r"мудак", "дурень")),
    ),
}


def _start(wl: WordList) -> str:
    """Regex for the start of a listed word (stems with an optional prefix, exact words)."""
    prefixes = "|".join(map(re.escape, sorted(wl.prefixes, key=len, reverse=True)))
    stems = "|".join(map(re.escape, sorted(wl.stems, key=len, reverse=True)))
    parts = []
    if stems:
        parts.append(f"(?:{prefixes})?(?:{stems})\\w*" if prefixes else f"(?:{stems})\\w*")
    if wl.words:
        parts.append("|".join(map(re.escape, sorted(wl.words, key=len, reverse=True))) + r"(?!\w)")
    return "|".join(parts)


@cache
def _pattern(lang: str) -> re.Pattern | None:
    wl = LISTS.get(lang)
    if wl is None:
        return None
    return re.compile(rf"(?<!\w)(?:{_start(wl)})(?!\w)", re.IGNORECASE)


@cache
def _swaps(lang: str) -> list[tuple[re.Pattern, str]]:
    wl = LISTS.get(lang, WordList())
    return [(re.compile(rf"(?<!\w)(?:{p})(?!\w)", re.IGNORECASE), r) for p, r in wl.swaps]


def has_swearing(text: str, lang: str) -> bool:
    pattern = _pattern(lang)
    return bool(pattern and pattern.search(text))


def _match_case(original: str, replacement: str) -> str:
    if original[:1].isupper() and replacement:
        return replacement[0].upper() + replacement[1:]
    return replacement


GAP = chr(0)  # where a word was left out


def _tidy(text: str) -> str:
    text = re.sub(rf"\s*,\s*{GAP}\s*,\s*", " ", text)  # "was, bloody, shocked" → "was shocked"
    text = text.replace(GAP, "")
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)  # "crazy ." → "crazy."
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)  # ", ," → ","
    text = re.sub(r"^[\s,;:]+", "", text)  # a sentence that began with the dropped word
    text = re.sub(r"[,;:]\s*([.!?])", r"\1", text)  # "true, !" → "true!"
    text = re.sub(r"\(\s*\)|\"\s*\"", "", text)
    return text.strip()


def clean(text: str, lang: str) -> str:
    """The translation with swear words swapped for a mild word or left out."""
    pattern = _pattern(lang)
    if pattern is None or not pattern.search(text):
        return text
    starts_upper = text[:1].isupper()
    for swap, replacement in _swaps(lang):
        text = swap.sub(lambda m, r=replacement: _match_case(m.group(0), m.expand(r)), text)
    text = _tidy(pattern.sub(GAP, text))
    if starts_upper and text[:1].islower():
        text = text[0].upper() + text[1:]
    return text


def blocked_sequences(lang: str, vocab: dict[str, int], encode) -> list[list[str]]:
    """Token sequences NLLB must not produce in this language.

    vocab is the SentencePiece vocabulary ("▁word" starts a word), encode(text) gives the
    tokens for a text. Covers whole-word tokens ("▁fucking") and words split in pieces
    ("▁F", "UC", "K"); suppressing a sequence also blocks every longer word that starts with it.
    """
    wl = LISTS.get(lang)
    if wl is None:
        return []
    stems = re.compile(_start(WordList(stems=wl.stems, prefixes=wl.prefixes)), re.IGNORECASE)
    blocked: set[tuple[str, ...]] = set()
    for token in vocab:
        if token.startswith("▁") and len(token) > 2 and stems.fullmatch(token[1:]):
            blocked.add((token,))
    # Only stems: blocking "хер" would also block "херувим". Exact words are left to clean().
    for word in wl.stems:
        for form in {word, word.capitalize(), word.upper()}:
            for text in (form, f"x {form}"):  # at the start, and after another word
                tokens = encode(text)
                if text.startswith("x "):
                    tokens = tokens[1:]
                if tokens:
                    blocked.add(tuple(tokens))
    return [list(t) for t in blocked]
