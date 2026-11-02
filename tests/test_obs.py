import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from livetranslate.config import ObsSettings, Settings
from livetranslate.integrations.obs import AUTHENTICATION_FAILED, ObsOutput, authentication


class FakeObs:
    """Speaks the part of OBS's WebSocket protocol the app uses, including the password check."""

    def __init__(self, password: str = ""):
        self.password = password
        self.salt, self.challenge = "c2FsdA==", "Y2hhbGxlbmdl"
        self.requests: list[tuple[str, dict]] = []
        self.enabled = {("Main", 7): True}
        self.logins = 0
        self.server = None
        self.port = 0

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, socket):
        hello = {"obsWebSocketVersion": "5.6", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"salt": self.salt, "challenge": self.challenge}
        await socket.send(json.dumps({"op": 0, "d": hello}))
        identify = json.loads(await socket.recv())
        assert identify["op"] == 1
        if self.password and identify["d"].get("authentication") != authentication(self.password, self.salt, self.challenge):
            await socket.close(AUTHENTICATION_FAILED, "Authentication failed.")
            return
        self.logins += 1
        await socket.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for raw in socket:
            d = json.loads(raw)["d"]
            kind, data = d["requestType"], d.get("requestData") or {}
            self.requests.append((kind, data))
            result, code, comment, reply = True, 100, None, {}
            if kind == "GetVersion":
                reply = {"obsVersion": "32.2.1"}
            elif kind == "GetInputList":
                reply = {"inputs": [{"inputName": "Subtitles", "inputKind": "text_gdiplus_v3"}, {"inputName": "Camera", "inputKind": "dshow_input"}]}
            elif kind == "GetSceneList":
                reply = {"currentProgramSceneName": "Main", "scenes": [{"sceneName": "Other"}, {"sceneName": "Main"}]}
            elif kind == "GetCurrentProgramScene":
                reply = {"currentProgramSceneName": "Main"}
            elif kind == "GetSceneItemId":
                if data["sceneName"] == "Main" and data["sourceName"] == "Subtitles":
                    reply = {"sceneItemId": 7}
                else:
                    result, code, comment = False, 600, "No scene items were found in the specified scene."
            elif kind == "GetSceneItemEnabled":
                reply = {"sceneItemEnabled": self.enabled[(data["sceneName"], data["sceneItemId"])]}
            elif kind == "SetSceneItemEnabled":
                self.enabled[(data["sceneName"], data["sceneItemId"])] = data["sceneItemEnabled"]
            status = {"result": result, "code": code}
            if comment:
                status["comment"] = comment
            response = {"requestType": kind, "requestId": d["requestId"], "requestStatus": status, "responseData": reply}
            await socket.send(json.dumps({"op": 7, "d": response}))

    def texts(self):
        return [d["inputSettings"]["text"] for kind, d in self.requests if kind == "SetInputSettings"]


def output_for(fake, **settings):
    obs = ObsSettings(host="127.0.0.1", port=fake.port, **settings)
    return ObsOutput(lambda: Settings(obs=obs))


def test_authentication_matches_the_protocols_recipe():
    # Worked out by hand from the protocol: base64(sha256(base64(sha256(password + salt)) + challenge)).
    assert authentication("supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=") == (
        "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="
    )


async def test_lists_only_text_sources_and_the_scenes_as_obs_shows_them():
    async with FakeObs() as fake:
        output = output_for(fake)
        state = await output.state()
        await output.close()
    assert state == {"version": "32.2.1", "inputs": ["Subtitles"], "scenes": ["Main", "Other"], "current_scene": "Main"}
    assert output.status["ok"] is True


async def test_password_is_checked_and_a_wrong_one_says_so():
    async with FakeObs(password="secret") as fake:
        good = output_for(fake, password="secret")
        assert await good.state() is not None
        await good.close()

        wrong = output_for(fake, password="nope")
        assert await wrong.state() is None
        assert wrong.status == {"ok": False, "error": "The password is wrong", "last_text": "", "overlay": None}
        await wrong.close()

        none = output_for(fake)
        assert await none.state() is None
        assert none.status["error"] == "OBS asks for a password"
        await none.close()


async def test_subtitles_roll_and_only_when_switched_on():
    async with FakeObs() as fake:
        output = output_for(fake, source="Subtitles", enabled=True)
        await output.push("First sentence.")
        await output.push("Second sentence.")
        await output.clear()
        await output.close()

        off = output_for(fake, source="Subtitles", enabled=False)
        await off.push("Not this one.")
        await off.close()
    assert fake.texts() == ["First sentence.", "First sentence.\nSecond sentence.", ""]
    assert fake.logins == 1  # one connection for all of them; "off" never connected


async def test_overlay_uses_the_scene_on_air_or_the_chosen_one():
    async with FakeObs() as fake:
        output = output_for(fake, source="Subtitles")
        assert (await output.refresh())["overlay"] is True  # the switch starts at what OBS really shows
        assert await output.overlay(False) is True
        assert fake.enabled[("Main", 7)] is False
        assert output.status["overlay"] is False
        await output.close()

        chosen = output_for(fake, source="Subtitles", scene="Other")
        assert await chosen.overlay(True) is False
        assert chosen.status["error"] == "Scene “Other” doesn't have the source “Subtitles”"
        await chosen.close()


async def test_obs_going_away_is_reported_and_it_reconnects():
    fake = FakeObs()
    async with fake:
        port = fake.port
        output = output_for(fake, source="Subtitles", enabled=True)
        assert await output.show("One") is True
        fake.server.close()  # OBS closes
        await fake.server.wait_closed()
        assert await output.show("Two") is False
        assert output.status["ok"] is False and output.status["error"]

        # Started again, on the same port: the next sentence just works, and nothing was raised.
        fake.server = await serve(fake.handle, "127.0.0.1", port)
        assert await output.show("Three") is True
        assert output.status["ok"] is True
        await output.close()
    assert fake.texts()[-1] == "Three"


async def test_no_source_chosen_sends_nothing():
    async with FakeObs() as fake:
        output = output_for(fake, enabled=True)
        assert output.active is False
        assert await output.show("Hello") is False
        assert await output.overlay(True) is False
        await output.close()
    assert fake.requests == []


def test_existing_installs_keep_vmix_on_the_main_screen_new_ones_start_without(tmp_path):
    from livetranslate.config import SettingsStore

    assert Settings().main_streaming == "none"
    old = tmp_path / "old.json"
    old.write_text('{"source_language": "nl"}')
    assert SettingsStore(old).settings.main_streaming == "vmix"
    assert "obs" not in __import__("livetranslate.config", fromlist=["PROFILE_FIELDS"]).PROFILE_FIELDS  # no passwords in a shared file


async def test_both_programs_get_the_sentence_in_their_own_language(tmp_path):
    from livetranslate.config import SettingsStore, VmixSettings
    from livetranslate.server.app import create_app

    from tests.test_vmix import FakeVmix

    async with FakeObs() as fake:
        store = SettingsStore(tmp_path / "settings.json")
        store.save(
            Settings(
                setup_done=True,
                source_language="nl",
                target_languages=["en", "de"],
                vmix=VmixSettings(enabled=True, input="bbb-222", field="Headline.Text"),
                obs=ObsSettings(enabled=True, port=fake.port, source="Subtitles", language="de"),
            )
        )
        services = create_app(store, tmp_path, manage_translator=False).state.services
        vmix = FakeVmix()
        services.vmix._transport = vmix.transport

        async def translate(text, source, target):
            return f"[{target}] {text}"

        services.translator.translate = translate
        await services.pipeline._handle_sentence("Welkom", 0.0, "nl")
        await services.obs.close()
        await services.vmix.close()
    assert fake.texts() == ["[de] Welkom"]
    assert any(c.get("Value") == "Welkom" for c in vmix.calls)
