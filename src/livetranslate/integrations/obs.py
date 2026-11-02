"""Send translated subtitles to a text source in OBS Studio, through its WebSocket API.

OBS 28 and newer: Tools → WebSocket Server Settings → Enable WebSocket server (default port 4455).
The text goes into a Text source; showing and hiding it is that source's eye in a scene, which is
what an overlay is in vMix. Protocol: github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..config import ObsSettings, Settings
from .subtitles import SubtitleOutput

log = logging.getLogger(__name__)

TIMEOUT_S = 3
RPC_VERSION = 1
AUTHENTICATION_FAILED = 4009  # how OBS closes the connection on a wrong password
NOT_FOUND = 600
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 6, 7


class ObsError(Exception):
    pass


def _sha256_b64(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def authentication(password: str, salt: str, challenge: str) -> str:
    return _sha256_b64(_sha256_b64(password + salt) + challenge)


class ObsClient:
    def __init__(self, host: str, port: int, password: str):
        self.key = (host, port, password)
        self.url = f"ws://{host}:{port}"
        self.password = password
        self._socket = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        try:
            self._socket = await connect(self.url, open_timeout=TIMEOUT_S, max_size=None, proxy=None)
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise ObsError("OBS isn't running, or its WebSocket server is off") from exc
        hello = await self._receive(OP_HELLO)
        identify: dict = {"rpcVersion": RPC_VERSION, "eventSubscriptions": 0}  # requests only, no events
        challenge = hello.get("authentication")
        if challenge:
            if not self.password:
                raise ObsError("OBS asks for a password")
            identify["authentication"] = authentication(self.password, challenge["salt"], challenge["challenge"])
        await self._socket.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
        await self._receive(OP_IDENTIFIED)

    async def _receive(self, op: int, request_id: str | None = None) -> dict:
        while True:
            try:
                message = json.loads(await asyncio.wait_for(self._socket.recv(), TIMEOUT_S))
            except ConnectionClosed as exc:
                if exc.rcvd is not None and exc.rcvd.code == AUTHENTICATION_FAILED:
                    raise ObsError("The password is wrong") from exc
                raise
            if message.get("op") == op and (request_id is None or message["d"].get("requestId") == request_id):
                return message["d"]

    async def _exchange(self, request_type: str, data: dict) -> dict:
        if self._socket is None:
            await self._connect()
        request_id = uuid.uuid4().hex
        request = {"requestType": request_type, "requestId": request_id, "requestData": data}
        await self._socket.send(json.dumps({"op": OP_REQUEST, "d": request}))
        response = await self._receive(OP_RESPONSE, request_id)
        status = response["requestStatus"]
        if not status["result"]:
            error = ObsError(status.get("comment") or f"{request_type} failed ({status['code']})")
            error.code = status["code"]
            raise error
        return response.get("responseData") or {}

    async def request(self, request_type: str, **data) -> dict:
        async with self._lock:
            try:
                return await self._exchange(request_type, data)
            except ConnectionClosed:
                # OBS was restarted since the last sentence; one new connection before giving up.
                await self._drop()
            except ObsError as exc:
                if getattr(exc, "code", None) is None:  # a failed login: don't keep a half-open connection
                    await self._drop()
                raise
            except (OSError, TimeoutError, WebSocketException):
                await self._drop()
                raise
            try:
                return await self._exchange(request_type, data)
            except (OSError, TimeoutError, WebSocketException, ObsError):
                await self._drop()
                raise

    async def _drop(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.close()
            except (OSError, WebSocketException):
                pass

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


class ObsOutput(SubtitleOutput):
    name = "OBS"
    errors = (OSError, TimeoutError, WebSocketException, ObsError)

    def __init__(self, get_settings: Callable[[], Settings]):
        super().__init__()
        self._get_settings = get_settings
        self._client: ObsClient | None = None

    @property
    def settings(self) -> ObsSettings:
        return self._get_settings().obs

    @property
    def ready(self) -> bool:
        return bool(self.settings.source)

    def client(self) -> ObsClient:
        s = self.settings
        if self._client is None or self._client.key != (s.host, s.port, s.password):
            self._client = ObsClient(s.host, s.port, s.password)
        return self._client

    async def _set_text(self, text: str) -> None:
        settings = {"text": text}
        await self.client().request("SetInputSettings", inputName=self.settings.source, inputSettings=settings, overlay=True)

    async def _scene_item(self) -> tuple[str, int]:
        """Where the eye of the text source is: in the chosen scene, or else the one on air."""
        client = self.client()
        s = self.settings
        scene = s.scene or (await client.request("GetCurrentProgramScene"))["currentProgramSceneName"]
        try:
            found = await client.request("GetSceneItemId", sceneName=scene, sourceName=s.source)
        except ObsError as exc:
            if getattr(exc, "code", None) == NOT_FOUND:
                raise ObsError(f"Scene “{scene}” doesn't have the source “{s.source}”") from exc
            raise
        return scene, found["sceneItemId"]

    async def _set_overlay(self, on: bool) -> None:
        scene, item = await self._scene_item()
        await self.client().request("SetSceneItemEnabled", sceneName=scene, sceneItemId=item, sceneItemEnabled=on)

    async def state(self) -> dict | None:
        """What can be chosen in OBS; None when it can't be reached (see status)."""
        result: dict = {}

        async def fetch():
            client = self.client()
            version = await client.request("GetVersion")
            inputs = (await client.request("GetInputList"))["inputs"]
            scenes = await client.request("GetSceneList")
            result.update(
                version=version.get("obsVersion", ""),
                inputs=sorted(i["inputName"] for i in inputs if i.get("inputKind", "").startswith("text_")),
                # OBS lists them from the bottom of its list up.
                scenes=[scene["sceneName"] for scene in reversed(scenes["scenes"])],
                current_scene=scenes.get("currentProgramSceneName", ""),
            )

        return result if await self._guard(fetch()) else None

    async def refresh(self) -> dict:
        """Whether OBS can be reached and the text is showing, for the switches on the main screen."""

        async def fetch():
            scene, item = await self._scene_item()
            found = await self.client().request("GetSceneItemEnabled", sceneName=scene, sceneItemId=item)
            self.status["overlay"] = found["sceneItemEnabled"]

        if self.ready:
            await self._guard(fetch())
        return self.status

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
