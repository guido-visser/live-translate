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


class Settings(BaseModel):
    source_language: str = "nl"
    target_languages: list[str] = Field(default_factory=lambda: ["ru", "uk", "en"])
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
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
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
