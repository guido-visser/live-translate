"""Runs the real NLLB model when it has been downloaded (skipped otherwise, e.g. in CI)."""

import pytest

from livetranslate.config import Settings, data_dir
from livetranslate.translate.nllb import LANGUAGES, MODELS, NllbTranslator


def test_every_language_has_a_flores_code():
    for code, (flores, name) in LANGUAGES.items():
        assert len(code) == 2 and "_" in flores and name


@pytest.mark.skipif(
    not (data_dir() / "models" / "nllb" / MODELS["600M"].replace("/", "--") / "model.bin").exists(),
    reason="NLLB 600M not downloaded",
)
async def test_dutch_to_ukrainian():
    translator = NllbTranslator(Settings(), data_dir())
    await translator.start()
    assert translator.status == "ready"
    result = await translator.translate("Laten we samen bidden voor de vrede in Oekraïne.", "nl", "uk")
    assert "Україн" in result and "мол" in result


@pytest.mark.skipif(
    not (data_dir() / "models" / "nllb" / MODELS["600M"].replace("/", "--") / "model.bin").exists(),
    reason="NLLB 600M not downloaded",
)
async def test_no_swearing_where_the_speaker_did_not():
    settings = Settings()
    translator = NllbTranslator(settings, data_dir())
    await translator.start()
    sentence = "Ik was verdomme gek."  # came out as "I was fucking crazy"
    assert "fuck" not in (await translator.translate(sentence, "nl", "en")).lower()
    settings.translation.clean_language = False
    translator.configure(settings)
    assert "fuck" in (await translator.translate(sentence, "nl", "en")).lower()
