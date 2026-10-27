"""Meta's NLLB-200 translation model, run locally with CTranslate2.

NLLB translates directly between languages (no detour through English), which
gives much better Dutch → Ukrainian than the Argos models in LibreTranslate.
License: CC-BY-NC 4.0 (fine for non-commercial use such as a church service).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)

MODELS = {
    "600M": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
    "1.3B": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
}

# ISO 639-1 (as used in the rest of the app) → NLLB/FLORES-200 codes, with English names.
LANGUAGES: dict[str, tuple[str, str]] = {
    "af": ("afr_Latn", "Afrikaans"),
    "am": ("amh_Ethi", "Amharic"),
    "ar": ("arb_Arab", "Arabic"),
    "az": ("azj_Latn", "Azerbaijani"),
    "be": ("bel_Cyrl", "Belarusian"),
    "bg": ("bul_Cyrl", "Bulgarian"),
    "bn": ("ben_Beng", "Bengali"),
    "bs": ("bos_Latn", "Bosnian"),
    "cs": ("ces_Latn", "Czech"),
    "da": ("dan_Latn", "Danish"),
    "de": ("deu_Latn", "German"),
    "el": ("ell_Grek", "Greek"),
    "en": ("eng_Latn", "English"),
    "es": ("spa_Latn", "Spanish"),
    "et": ("est_Latn", "Estonian"),
    "fa": ("pes_Arab", "Persian"),
    "fi": ("fin_Latn", "Finnish"),
    "fr": ("fra_Latn", "French"),
    "ha": ("hau_Latn", "Hausa"),
    "he": ("heb_Hebr", "Hebrew"),
    "hi": ("hin_Deva", "Hindi"),
    "hr": ("hrv_Latn", "Croatian"),
    "hu": ("hun_Latn", "Hungarian"),
    "hy": ("hye_Armn", "Armenian"),
    "id": ("ind_Latn", "Indonesian"),
    "ig": ("ibo_Latn", "Igbo"),
    "it": ("ita_Latn", "Italian"),
    "ja": ("jpn_Jpan", "Japanese"),
    "ka": ("kat_Geor", "Georgian"),
    "kk": ("kaz_Cyrl", "Kazakh"),
    "km": ("khm_Khmr", "Khmer"),
    "ko": ("kor_Hang", "Korean"),
    "ku": ("kmr_Latn", "Kurdish (Kurmanji)"),
    "ln": ("lin_Latn", "Lingala"),
    "lt": ("lit_Latn", "Lithuanian"),
    "lv": ("lvs_Latn", "Latvian"),
    "mk": ("mkd_Cyrl", "Macedonian"),
    "ml": ("mal_Mlym", "Malayalam"),
    "ms": ("zsm_Latn", "Malay"),
    "my": ("mya_Mymr", "Burmese"),
    "nb": ("nob_Latn", "Norwegian Bokmål"),
    "ne": ("npi_Deva", "Nepali"),
    "nl": ("nld_Latn", "Dutch"),
    "pa": ("pan_Guru", "Punjabi"),
    "pl": ("pol_Latn", "Polish"),
    "ps": ("pbt_Arab", "Pashto"),
    "pt": ("por_Latn", "Portuguese"),
    "ro": ("ron_Latn", "Romanian"),
    "ru": ("rus_Cyrl", "Russian"),
    "sk": ("slk_Latn", "Slovak"),
    "sl": ("slv_Latn", "Slovenian"),
    "so": ("som_Latn", "Somali"),
    "sq": ("als_Latn", "Albanian"),
    "sr": ("srp_Cyrl", "Serbian"),
    "sv": ("swe_Latn", "Swedish"),
    "sw": ("swh_Latn", "Swahili"),
    "ta": ("tam_Taml", "Tamil"),
    "tg": ("tgk_Cyrl", "Tajik"),
    "th": ("tha_Thai", "Thai"),
    "ti": ("tir_Ethi", "Tigrinya"),
    "tl": ("tgl_Latn", "Tagalog"),
    "tr": ("tur_Latn", "Turkish"),
    "uk": ("ukr_Cyrl", "Ukrainian"),
    "ur": ("urd_Arab", "Urdu"),
    "uz": ("uzn_Latn", "Uzbek"),
    "vi": ("vie_Latn", "Vietnamese"),
    "yo": ("yor_Latn", "Yoruba"),
    "zh": ("zho_Hans", "Chinese"),
    "zt": ("zho_Hant", "Chinese (Traditional)"),
}


class NllbTranslator:
    """Same interface as LibreTranslate, so the app can switch between them."""

    def __init__(self, settings: Settings, data_dir: Path):
        self.models_dir = data_dir / "models" / "nllb"
        self.status = "stopped"  # stopped|installing|starting|ready|error
        self.detail = ""
        self.languages = [
            {"code": code, "name": name, "targets": list(LANGUAGES)} for code, (_, name) in LANGUAGES.items()
        ]
        self.needs_restart = False
        self._translator = None
        self._tokenizer = None
        self._loaded: tuple[str, str] | None = None
        self._cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = asyncio.Lock()
        self._model_lock = threading.Lock()
        self.configure(settings)

    def configure(self, settings: Settings) -> None:
        self.model = settings.translation.nllb_model
        self.device = settings.translation.nllb_device
        self.needs_restart = self._loaded is not None and self._loaded != (self.model, self.device)

    async def start(self) -> None:
        async with self._lock:
            if self._loaded == (self.model, self.device) and self._translator is not None:
                self.status = "ready"
                return
            try:
                await asyncio.to_thread(self._load)
                self.status, self.detail = "ready", ""
            except Exception as exc:
                log.exception("NLLB failed to load")
                self.status, self.detail = "error", str(exc)

    def _load(self) -> None:
        import ctranslate2
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        from ..stt.cuda import cuda_available, setup_cuda_dll_paths

        repo = MODELS[self.model]
        folder = self.models_dir / repo.replace("/", "--")
        if not (folder / "model.bin").exists():
            self.status = "installing"
            self.detail = f"Downloading NLLB {self.model} (one time, needs internet)…"
            log.info("Downloading %s", repo)
            snapshot_download(repo, local_dir=folder, allow_patterns=["*.bin", "*.json", "*.txt", "*.model"])
        self.status, self.detail = "starting", f"Loading NLLB {self.model}…"

        setup_cuda_dll_paths()
        device = self.device
        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        compute_type = "int8_float16" if device == "cuda" else "int8"
        with self._model_lock:
            self._translator = None  # free memory before loading another size
            self._translator = ctranslate2.Translator(
                str(folder), device=device, compute_type=compute_type, inter_threads=2, intra_threads=4
            )
            self._tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self._loaded = (self.model, self.device)
        self.needs_restart = False
        log.info("NLLB %s ready on %s", self.model, device)

    async def stop(self) -> None:
        with self._model_lock:
            self._translator = None
            self._tokenizer = None
        self._loaded = None
        self.status = "stopped"

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def close(self) -> None:
        await self.stop()

    def targets_for(self, source: str) -> list[str]:
        return list(LANGUAGES) if source in LANGUAGES else []

    def name_of(self, code: str) -> str:
        return LANGUAGES.get(code, ("", code))[1]

    async def translate(self, text: str, source: str, target: str) -> str:
        if source == target or not text:
            return text
        key = (text, source, target)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        result = await asyncio.to_thread(self.translate_sync, text, source, target)
        self._cache[key] = result
        if len(self._cache) > 2000:
            self._cache.popitem(last=False)
        return result

    def translate_sync(self, text: str, source: str, target: str) -> str:
        translator, tokenizer = self._translator, self._tokenizer
        if translator is None or tokenizer is None:
            raise RuntimeError("NLLB is not loaded")
        src, tgt = LANGUAGES[source][0], LANGUAGES[target][0]
        tokens = [src, *tokenizer.encode(text, add_special_tokens=False).tokens, "</s>"]
        result = translator.translate_batch(
            [tokens],
            target_prefix=[[tgt]],
            beam_size=3,
            max_decoding_length=256,
            repetition_penalty=1.1,
        )[0]
        out = [t for t in result.hypotheses[0] if t != tgt]
        ids = [tokenizer.token_to_id(t) for t in out]
        return tokenizer.decode([i for i in ids if i is not None], skip_special_tokens=True).strip()
