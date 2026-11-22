from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from livetranslate.config import Settings, VmixSettings
from livetranslate.integrations.vmix import VmixOutput, compose, parse_state, tail, wrap

VMIX_XML = """<vmix>
  <version>27.0.0.49</version>
  <inputs>
    <input key="aaa-111" number="1" type="Capture" title="Camera 1" state="Running">Camera 1</input>
    <input key="bbb-222" number="2" type="GT" title="Ondertitels.gtzip" state="Paused">Ondertitels.gtzip
      <text index="0" name="Headline.Text">Hallo</text>
      <text index="1" name="Description.Text"></text>
    </input>
  </inputs>
  <overlays><overlay number="1">2</overlay><overlay number="2" /></overlays>
</vmix>"""


class FakeVmix:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError("connection refused", request=request)
        query = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query, keep_blank_values=True).items()}
        if not query:
            return httpx.Response(200, text=VMIX_XML)
        self.calls.append(query)
        return httpx.Response(200, text="Function completed successfully.")


def make_output(fake: FakeVmix, **vmix) -> VmixOutput:
    settings = Settings(vmix=VmixSettings(enabled=True, input="bbb-222", field="Headline.Text", **vmix))
    return VmixOutput(lambda: settings, transport=fake.transport)


def test_wrap_breaks_between_words():
    assert wrap("kort", 20) == ["kort"]
    assert wrap("dit is een hele lange zin die niet past", 20) == ["dit is een hele", "lange zin die niet", "past"]
    assert wrap("Godsvertrouwen", 5) == ["Godsv", "ertro", "uwen"]


def test_tail_keeps_the_end_in_two_lines():
    lines = tail("dit is een hele lange zin die niet past in twee regels van twintig", 20)
    assert len(lines) == 2 and all(len(line) <= 20 for line in lines)
    assert lines[0].startswith("…") and not lines[0][1:].startswith(" ")
    assert lines[-1].endswith("van twintig")


def test_compose_rolls_two_lines():
    assert compose(None, "Eerste zin.", 40) == "Eerste zin."
    assert compose("Eerste zin.", "Tweede zin.", 40) == "Eerste zin.\nTweede zin."
    # A current sentence of two lines leaves no room for the previous one.
    assert compose("Eerste zin.", "Een tweede zin die niet op één regel past.", 30) == (
        "Een tweede zin die niet op één\nregel past."
    )


@pytest.mark.parametrize("width", [20, 32, 42, 60])
def test_compose_never_gives_more_than_two_lines(width):
    words = ("De Heer is mijn herder, mij ontbreekt niets. Hij laat mij neerliggen in grazige weiden, "
             "Hij voert mij zachtjes naar rustige wateren.").split()
    previous = None
    for n in range(1, len(words) + 1):
        sentence = " ".join(words[:n])
        lines = compose(previous, sentence, width).split("\n")
        assert len(lines) <= 2 and all(len(line) <= width for line in lines), (width, sentence)
        previous = wrap(sentence, width)[0] if len(wrap(sentence, width)) == 1 else None


async def test_push_keeps_a_short_previous_sentence_only():
    fake = FakeVmix()
    out = make_output(fake, line_chars=30)
    await out.push("Welkom allemaal.")
    await out.push("Vandaag lezen we uit Psalm drieëntwintig, vers één.")  # two lines on its own
    await out.push("Amen.")  # the long one doesn't go above it
    assert [c["Value"] for c in fake.calls] == [
        "Welkom allemaal.",
        "Vandaag lezen we uit Psalm\ndrieëntwintig, vers één.",
        "Amen.",
    ]


def test_old_max_chars_setting_becomes_characters_per_line():
    old = {"enabled": True, "max_chars": 160}
    assert VmixSettings.model_validate(old).line_chars == 80
    assert VmixSettings.model_validate({**old, "line_chars": 42}).line_chars == 42
    assert VmixSettings().line_chars == 50


def test_parse_state_lists_title_inputs_and_overlays():
    state = parse_state(VMIX_XML)
    assert state["version"] == "27.0.0.49"
    assert state["inputs"] == [
        {
            "key": "bbb-222",
            "number": 2,
            "title": "Ondertitels.gtzip",
            "type": "GT",
            "fields": ["Headline.Text", "Description.Text"],
        }
    ]
    assert state["overlays"] == {1: "2", 2: None}


async def test_push_sends_rolling_text_and_overlay():
    fake = FakeVmix()
    out = make_output(fake, overlay=2)
    await out.push("Ik kom uit Charkiv.")
    await out.push("God heeft ons geholpen.")
    assert await out.overlay(True)
    assert await out.overlay(False)
    assert fake.calls == [
        {"Function": "SetText", "Input": "bbb-222", "SelectedName": "Headline.Text", "Value": "Ik kom uit Charkiv."},
        {
            "Function": "SetText",
            "Input": "bbb-222",
            "SelectedName": "Headline.Text",
            "Value": "Ik kom uit Charkiv.\nGod heeft ons geholpen.",
        },
        {"Function": "OverlayInput2In", "Input": "bbb-222"},
        {"Function": "OverlayInput2Out"},
    ]
    assert out.status["ok"] is True and out.status["overlay"] is False


async def test_unreachable_vmix_is_reported_not_raised():
    out = make_output(FakeVmix(fail=True))
    await out.push("Hallo")
    assert out.status["ok"] is False and "refused" in out.status["error"]
    assert await out.state() is None


async def test_inactive_output_sends_nothing():
    fake = FakeVmix()
    settings = Settings()  # vMix disabled
    out = VmixOutput(lambda: settings, transport=fake.transport)
    await out.push("Hallo")
    await out.clear()
    assert fake.calls == []
