"""Language names as speakers of that language write them, and the interface language."""

import locale
import os

NATIVE_NAMES = {
    "ar": "العربية",
    "bg": "Български",
    "cs": "Čeština",
    "da": "Dansk",
    "de": "Deutsch",
    "el": "Ελληνικά",
    "en": "English",
    "es": "Español",
    "et": "Eesti",
    "fa": "فارسی",
    "fi": "Suomi",
    "fr": "Français",
    "he": "עברית",
    "hi": "हिन्दी",
    "hu": "Magyar",
    "id": "Bahasa Indonesia",
    "it": "Italiano",
    "ja": "日本語",
    "ko": "한국어",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "nb": "Norsk bokmål",
    "nl": "Nederlands",
    "pl": "Polski",
    "pt": "Português",
    "ro": "Română",
    "ru": "Русский",
    "sk": "Slovenčina",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "sv": "Svenska",
    "th": "ไทย",
    "tl": "Tagalog",
    "tr": "Türkçe",
    "uk": "Українська",
    "ur": "اردو",
    "vi": "Tiếng Việt",
    "zh": "中文",
    "zt": "中文（繁體）",
}

RTL = {"ar", "fa", "he", "ur"}


def native_name(code: str, fallback: str | None = None) -> str:
    return NATIVE_NAMES.get(code, fallback or code)


UI_LANGUAGES = ("nl", "en", "ru", "uk")


def ui_language() -> str:
    """Interface language for the tray and the operator pages: the installer's language
    (passed on by the launcher), else the OS locale ("Dutch_Netherlands", "nl_NL", …)."""
    chosen = os.environ.get("LIVETRANSLATE_UI_LANG", "").strip().lower()
    if chosen in UI_LANGUAGES:
        return chosen
    try:
        name = (locale.getlocale()[0] or "").lower()
    except ValueError:
        name = ""
    for code, prefixes in (("nl", ("nl", "dutch")), ("ru", ("ru", "russian")), ("uk", ("uk", "ukrainian"))):
        if name.startswith(prefixes):
            return code
    return "en"
