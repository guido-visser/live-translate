import pytest

from livetranslate.config import GlossaryEntry, Settings, data_dir
from livetranslate.translate.glossary import Glossary
from livetranslate.translate.nllb import MODELS, NllbTranslator
from livetranslate.translate.service import TranslationService

ENTRIES = [
    GlossaryEntry(term="gemeente, gemeenten", translations={"ru": "церковь", "uk": "церква"}),
    GlossaryEntry(term="heilig avondmaal, avondmaal", translations={"ru": "Вечеря Господня"}),
    GlossaryEntry(term="Heer", translations={"ru": "Господь"}),
    GlossaryEntry(term="Jansen", translations={"ru": "Янсен", "uk": ""}),
    GlossaryEntry(lang="ru", term="церковь", translations={"nl": "kerk"}),
    GlossaryEntry(term="diaken", translations={"en": "deacon", "uk": "диякон"}),
]


def test_inline_puts_the_translation_in_the_sentence():
    glossary = Glossary(ENTRIES)
    text, terms = glossary.prepare("Welkom in de gemeente, ook de andere gemeenten.", "nl", "ru", inline=True)
    assert text == "Welkom in de церковь, ook de andere церковь."
    assert terms == []


def test_placeholders_and_restore():
    glossary = Glossary(ENTRIES)
    text, terms = glossary.prepare("Na het Heilig Avondmaal bidt Jansen.", "nl", "ru", inline=False)
    assert text == "Na het {1} bidt {2}."
    assert terms == ["Вечеря Господня", "Янсен"]
    # Engines change the brackets now and then; any of them is put back.
    assert Glossary.restore("После [1] молится {2}.", terms) == "После Вечеря Господня молится Янсен."
    assert Glossary.restore("После ужина молится {2}.", terms) is None
    assert Glossary.restore("After the {1} we sing.", ["the Lord's Supper"]) == "After the Lord's Supper we sing."


def test_matching_rules():
    glossary = Glossary(ENTRIES)
    prepare = lambda text: glossary.prepare(text, "nl", "ru", inline=True)[0]  # noqa: E731
    assert prepare("Gemeente, sta op.") == "церковь, sta op."  # lowercase entry: any case
    assert prepare("De gemeentelijke regels.") == "De gemeentelijke regels."  # whole words only
    assert prepare("Wij danken de Heer.") == "Wij danken de Господь."
    assert prepare("De heer Jansen.") == "De heer Янсен."  # "Heer" with a capital only
    assert prepare("Het heilig  avondmaal.") == "Het Вечеря Господня."  # longest form, any spacing


def test_empty_or_missing_translation_and_other_languages_are_left_alone():
    glossary = Glossary(ENTRIES)
    assert glossary.prepare("Jansen leest voor.", "nl", "uk", inline=True)[0] == "Jansen leest voor."
    assert glossary.prepare("Het avondmaal.", "nl", "en", inline=False) == ("Het avondmaal.", [])
    assert glossary.prepare("Наша церковь.", "ru", "nl", inline=True)[0] == "Наша kerk."
    assert glossary.prepare("Welkom.", "de", "ru", inline=True) == ("Welkom.", [])


def test_english_word_steers_languages_without_their_own_translation():
    glossary = Glossary(ENTRIES)
    # Always in the sentence itself, also for LibreTranslate: the engine must translate it.
    assert glossary.prepare("De diaken bidt.", "nl", "ru", inline=False) == ("De deacon bidt.", [])
    assert glossary.prepare("De diaken bidt.", "nl", "ru", inline=True) == ("De deacon bidt.", [])
    # A language's own translation wins over the English word.
    assert glossary.prepare("De diaken bidt.", "nl", "uk", inline=False) == ("De {1} bidt.", ["диякон"])
    assert glossary.prepare("De diaken bidt.", "nl", "en", inline=False) == ("De {1} bidt.", ["deacon"])


def test_hotwords_per_spoken_language_in_list_order():
    glossary = Glossary(ENTRIES)
    assert glossary.hotwords("nl") == "gemeente, gemeenten, heilig avondmaal, avondmaal, Heer, Jansen, diaken"
    assert glossary.hotwords("ru") == "церковь"
    assert glossary.hotwords("uk") == ""


def test_default_glossary_covers_the_usual_mistakes():
    glossary = Glossary(Settings().glossary)
    text, _ = glossary.prepare("Welkom in de gemeente, vandaag vieren we het avondmaal.", "nl", "ru", inline=True)
    assert text == "Welkom in de church, vandaag vieren we het the Lord's Supper."


def make_service(engine: str, reply) -> TranslationService:
    settings = Settings(glossary=ENTRIES)
    settings.translation.engine = engine
    service = TranslationService(settings, data_dir())
    calls = []

    async def translate(text, source, target):
        calls.append(text)
        return reply(text)

    service.active.translate = translate
    service.calls = calls
    return service


async def test_nllb_gets_the_word_in_the_sentence():
    service = make_service("nllb", lambda text: text.replace("Welkom in de", "Добро пожаловать в"))
    assert await service.translate("Welkom in de gemeente.", "nl", "ru") == "Добро пожаловать в церковь."
    assert service.calls == ["Welkom in de церковь."]


async def test_libretranslate_restores_placeholders_or_falls_back():
    service = make_service("libretranslate", lambda text: text.replace("Welkom in de", "Добро пожаловать в"))
    assert await service.translate("Welkom in de gemeente.", "nl", "ru") == "Добро пожаловать в церковь."
    assert service.calls == ["Welkom in de {1}."]

    lossy = make_service("libretranslate", lambda text: "Добро пожаловать.")
    assert await lossy.translate("Welkom in de gemeente.", "nl", "ru") == "Добро пожаловать."
    assert lossy.calls == ["Welkom in de {1}.", "Welkom in de gemeente."]


@pytest.mark.skipif(
    not (data_dir() / "models" / "nllb" / MODELS["600M"].replace("/", "--") / "model.bin").exists(),
    reason="NLLB 600M not downloaded",
)
async def test_real_nllb_uses_the_glossary():
    settings = Settings()
    settings.translation.engine = "nllb"
    service = TranslationService(settings, data_dir())
    assert isinstance(service.active, NllbTranslator)
    await service.start()
    without = await service.nllb.translate("Welkom in de gemeente.", "nl", "ru")
    assert "церк" not in without.lower()  # NLLB alone says "municipality"
    assert "церк" in (await service.translate("Welkom in de gemeente.", "nl", "ru")).lower()
    await service.close()
