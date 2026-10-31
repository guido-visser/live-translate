"""Persistent settings. Only settings are stored on disk, never transcripts."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field

APP_NAME = "LiveTranslate"

log = logging.getLogger(__name__)


def data_dir() -> Path:
    """Folder for downloaded models, the LibreTranslate environment and logs."""
    path = Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    path = Path(user_config_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path / "settings.json"


class AudioSettings(BaseModel):
    kind: Literal["device", "loopback", "file"] = "device"
    # Stable device id ("<host api>::<name>"); None means the system default input.
    device: str | None = None
    # 0 = mix all channels to mono, otherwise the 1-based channel to use.
    channel: int = 0
    file_path: str = ""
    gain: float = 1.0


class AsrSettings(BaseModel):
    model: str = "large-v3-turbo"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    beam_size: int = 1
    # Seconds of new audio to collect before re-running recognition.
    min_chunk_s: float = 1.0
    # Silence after speech that finalizes the current sentence.
    silence_commit_s: float = 0.6
    # Force a sentence out after this long without punctuation or a pause.
    max_sentence_s: float = 8.0


class TranslationSettings(BaseModel):
    engine: Literal["libretranslate", "nllb"] = "libretranslate"
    nllb_model: Literal["600M", "1.3B"] = "600M"
    # CPU by default so the GPU stays free for speech recognition and vMix.
    nllb_device: Literal["cpu", "cuda", "auto"] = "cpu"


class VmixSettings(BaseModel):
    """Subtitles sent to a vMix Title input (via the vMix Web API)."""

    enabled: bool = False
    url: str = "http://127.0.0.1:8088"
    input: str = ""  # vMix input key
    field: str = ""  # text field in that title, e.g. "Headline.Text"
    # Language shown in vMix; empty = the main language. Speech in another language is translated.
    language: str = ""
    overlay: int = Field(1, ge=1, le=16)
    max_chars: int = Field(160, ge=20, le=1000)
    # On a spoken-language switch: overlay in when vMix shows a translation, out when it shows the original.
    auto_overlay: bool = True


class GlossaryEntry(BaseModel):
    """A word the speech recognition should know, optionally with fixed translations."""

    lang: str = "nl"  # language the word is spoken in
    # One or more forms, comma-separated ("gemeente, gemeenten"). A form with a capital
    # letter only matches with that capital ("Heer" is the Lord, "heer" is Mr.).
    term: str
    # Language code → the translation to always use. "en" also serves as the hint for
    # languages left empty; with no English either, the word is translated normally.
    translations: dict[str, str] = Field(default_factory=dict)


def default_glossary() -> list[GlossaryEntry]:
    """Dutch church words the translation engines tend to get wrong ("gemeente" → municipality).

    English only: the English word steers every other language. These English words
    tested best inside Dutch sentences with NLLB ("donations", not "offering" = sacrifice).
    """
    rows = [
        ("gemeente", "church"),
        ("heilig avondmaal, avondmaal", "the Lord's Supper"),
        ("dominee, voorganger", "pastor"),
        ("ouderling", "elder"),
        ("diaken", "deacon"),
        ("collecte", "donations"),
    ]
    return [GlossaryEntry(term=term, translations={"en": en}) for term, en in rows]


class Settings(BaseModel):
    source_language: str = "nl"
    target_languages: list[str] = Field(default_factory=lambda: ["ru", "uk", "en"])
    # Languages offered as quick "spoken language" buttons next to the main language.
    quick_languages: list[str] = Field(default_factory=lambda: ["ru", "uk"])
    tts_enabled: bool = True
    port: int = 8000
    # Override for the address in the QR code (e.g. when the PC has several networks).
    public_host: str = ""
    wifi_name: str = ""
    # When set, the admin page may also be opened from other devices with this PIN.
    admin_pin: str = ""
    # When set, use this LibreTranslate instead of starting one ourselves.
    libretranslate_url: str = ""
    libretranslate_port: int = 5001
    # Look for a new version on GitHub now and then (installing is always the operator's choice).
    check_updates: bool = True
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
    glossary: list[GlossaryEntry] = Field(default_factory=default_glossary)
    vmix: VmixSettings = Field(default_factory=VmixSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    asr: AsrSettings = Field(default_factory=AsrSettings)


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        self._lock = threading.Lock()
        self.settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                return Settings.model_validate(json.loads(self.path.read_text("utf-8")))
            except Exception:
                log.exception("Could not read %s, using defaults", self.path)
        return Settings()

    def save(self, settings: Settings) -> None:
        with self._lock:
            self.settings = settings
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(settings.model_dump_json(indent=2), "utf-8")
            tmp.replace(self.path)
