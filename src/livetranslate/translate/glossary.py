"""Fixed translations for church words and names, and the word list for Whisper.

English first: the operator fills in the English word, and for every language
without its own translation that English word goes into the sentence as a hint.
Both engines translate it from there ("Welkom in de church" → "в церковь"), and
inflect it properly. A translation filled in for a language wins over the hint.

For those fixed translations the engines need different tricks (tested on real sentences):

* NLLB: the target word is put straight into the source sentence. NLLB copies it
  and even declines it ("het Вечеря Господня" → "Вечери Господней"), which
  placeholders can't do; it drops or garbles those.
* LibreTranslate (Argos, via English): Cyrillic in a Dutch sentence gets lost on
  the way through English, but "{1}" survives both steps, so the word is put back
  afterwards (undeclined).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..config import GlossaryEntry

HINT = "en"  # the language whose word stands in for languages without their own translation
PLACEHOLDER = re.compile(r"[{\[(]\s*(\d+)\s*[}\])]")
DOUBLE_ARTICLE = re.compile(r"\b(?:the|a|an)\s+(the|a|an)\b(?=\s)", re.IGNORECASE)


def _forms(term: str) -> list[str]:
    return [f.strip() for f in term.split(",") if f.strip()]


def _form_pattern(form: str) -> str:
    pattern = r"\s+".join(re.escape(part) for part in form.split())
    # Lowercase forms match any capitalisation (also at the start of a sentence);
    # forms with a capital only match exactly, so "heer" (Mr.) is not "Heer" (Lord).
    return f"(?i:{pattern})" if form == form.lower() else pattern


class Glossary:
    def __init__(self, entries: Iterable[GlossaryEntry] = ()):
        self._patterns: dict[str, tuple[re.Pattern, list[GlossaryEntry]]] = {}
        self._hotwords: dict[str, str] = {}
        by_lang: dict[str, list[tuple[str, GlossaryEntry]]] = {}
        for entry in entries:
            for form in _forms(entry.term):
                by_lang.setdefault(entry.lang, []).append((form, entry))
        for lang, forms in by_lang.items():
            # In list order: Whisper only reads the start of a long list.
            self._hotwords[lang] = ", ".join(dict.fromkeys(form for form, _ in forms))
            forms.sort(key=lambda f: len(f[0]), reverse=True)  # "Heilig Avondmaal" before "avondmaal"
            alternatives = "|".join(f"(?P<g{i}>{_form_pattern(form)})" for i, (form, _) in enumerate(forms))
            self._patterns[lang] = (re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)"), [e for _, e in forms])

    def hotwords(self, lang: str) -> str:
        """The words to prime Whisper with when this language is spoken."""
        return self._hotwords.get(lang, "")

    def prepare(self, text: str, source: str, target: str, inline: bool) -> tuple[str, list[str]]:
        """Mark the glossary words in a sentence before it is translated.

        inline=True puts the translations in the sentence itself; otherwise they become
        numbered placeholders, returned in order for restore(). The English hint always
        goes in the sentence itself: the engine has to translate it.
        """
        found = self._patterns.get(source)
        if found is None:
            return text, []
        pattern, entries = found
        terms: list[str] = []

        def replace(match: re.Match) -> str:
            entry = entries[int(match.lastgroup[1:])]
            translation = entry.translations.get(target, "").strip()
            if not translation:
                hint = entry.translations.get(HINT, "").strip()
                return hint if hint and source != HINT else match.group(0)
            if inline:
                return translation
            terms.append(translation)
            return f"{{{len(terms)}}}"

        return pattern.sub(replace, text), terms

    @staticmethod
    def restore(text: str, terms: list[str]) -> str | None:
        """Put the words back in place of the placeholders; None when the engine lost one."""
        seen: set[int] = set()

        def replace(match: re.Match) -> str:
            n = int(match.group(1))
            if not 1 <= n <= len(terms):
                return match.group(0)
            seen.add(n)
            return terms[n - 1]

        restored = PLACEHOLDER.sub(replace, text)
        # "the {1}" with {1} = "the Lord's Supper" must not become "the the Lord's Supper".
        restored = DOUBLE_ARTICLE.sub(r"\1", restored)
        return restored if len(seen) == len(terms) else None
