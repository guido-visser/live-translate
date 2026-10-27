from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from livetranslate.config import Settings, VmixSettings
from livetranslate.integrations.vmix import VmixOutput, compose, parse_state, tail

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


def test_tail_cuts_at_word_boundary():
    assert tail("kort", 20) == "kort"
    cut = tail("dit is een hele lange zin die niet past", 20)
    assert cut.startswith("…") and len(cut) <= 20 and cut.endswith("niet past")
    assert not cut[1:].startswith(" ")


def test_compose_rolls_two_lines():
    assert compose(None, "Eerste zin.", 80) == "Eerste zin."
    assert compose("Eerste zin.", "Tweede zin.", 80) == "Eerste zin.\nTweede zin."
    assert compose("Een vorige zin die lang is.", "Nieuwe zin.", 30) == "Nieuwe zin."


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
    assert await out.inputs() is None


async def test_inactive_output_sends_nothing():
    fake = FakeVmix()
    settings = Settings()  # vMix disabled
    out = VmixOutput(lambda: settings, transport=fake.transport)
    await out.push("Hallo")
    await out.clear()
    assert fake.calls == []
