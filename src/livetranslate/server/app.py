"""HTTP + WebSocket server: listener pages for phones, the operator page, audio streams."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..audio.devices import list_all
from ..config import Settings, SettingsStore, data_dir as default_data_dir
from ..integrations.vmix import VmixOutput
from ..languages import RTL, native_name
from ..pipeline import Pipeline
from ..translate.service import TranslationService
from ..tts.piper import SpeechService
from .hub import Connection, Hub
from .network import GpuStats, lan_addresses, listener_url, qr_svg

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent.parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
WHISPER_MODELS = ["large-v3-turbo", "large-v3", "medium", "small", "base"]


class Services:
    def __init__(self, store: SettingsStore, data: Path):
        self.store = store
        self.data = data
        self.hub = Hub()
        self.translator = TranslationService(store.settings, data)
        self.speech = SpeechService(data / "models" / "piper")
        self.speech.enabled = store.settings.tts_enabled
        self.vmix = VmixOutput(lambda: store.settings)
        self.pipeline = Pipeline(store, self.translator, self.speech, self.hub, data, self.vmix)
        self.gpu = GpuStats()

    @property
    def settings(self) -> Settings:
        return self.store.settings

    def listener_languages(self) -> list[dict]:
        source = self.settings.source_language
        available = set(self.translator.targets_for(source))
        codes = [source] + [
            t for t in self.settings.target_languages if t != source and (not available or t in available)
        ]
        return [
            {
                "code": c,
                "name": native_name(c, self.translator.name_of(c)),
                "tts": self.speech.has_voice(c),
                "rtl": c in RTL,
            }
            for c in codes
        ]

    def languages_message(self) -> dict:
        return {
            "type": "languages",
            "languages": self.listener_languages(),
            "source": self.settings.source_language,
        }


def create_app(store: SettingsStore | None = None, data: Path | None = None, manage_translator: bool = True) -> FastAPI:
    store = store or SettingsStore()
    services = Services(store, data or default_data_dir())

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        services.pipeline.ensure_worker()
        tasks = [asyncio.create_task(_admin_ticker(services))]
        if manage_translator:
            tasks.append(asyncio.create_task(_start_translator(services)))
        yield
        for task in tasks:
            task.cancel()
        await services.pipeline.stop()
        await services.translator.close()
        await services.vmix.close()

    app = FastAPI(title="Live Translate", lifespan=lifespan)
    app.state.services = services
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def require_admin(request: Request | WebSocket) -> None:
        host = request.client.host if request.client else ""
        pin = services.settings.admin_pin
        if host in LOCAL_HOSTS or (pin and request.query_params.get("pin") == pin):
            return
        raise HTTPException(403, "The admin page is only available on this computer.")

    # -- pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def listener_page():
        return FileResponse(WEB / "client" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB / "client" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request):
        try:
            require_admin(request)
        except HTTPException as exc:
            return HTMLResponse(f"<h1>403</h1><p>{exc.detail}</p>", status_code=403)
        return FileResponse(WEB / "admin" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/qr", include_in_schema=False)
    async def qr_page():
        return FileResponse(WEB / "admin" / "qr.html", headers={"Cache-Control": "no-cache"})

    # -- public API ------------------------------------------------------------

    @app.get("/api/public")
    async def public_info():
        s = services.settings
        return {
            "url": listener_url(s.public_host, s.port),
            "wifi": s.wifi_name,
            "languages": services.listener_languages(),
        }

    @app.get("/api/qr.svg")
    async def qr():
        s = services.settings
        svg = qr_svg(listener_url(s.public_host, s.port))
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    @app.get("/audio/{lang}.mp3")
    async def audio_stream(lang: str, request: Request):
        if not services.speech.has_voice(lang):
            raise HTTPException(404, "No voice for this language")
        if lang not in {l["code"] for l in services.listener_languages()}:
            raise HTTPException(404, "Language not offered")
        asyncio.create_task(services.speech.warm_up(lang))
        stream = services.speech.stream(lang)
        queue = stream.subscribe()

        async def body():
            try:
                while True:
                    yield await queue.get()
            finally:
                stream.unsubscribe(queue)

        headers = {"Cache-Control": "no-cache, no-store", "X-Content-Type-Options": "nosniff"}
        return StreamingResponse(body(), media_type="audio/mpeg", headers=headers)

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket):
        await ws.accept()
        conn = Connection(ws)
        services.hub.listeners.add(conn)
        pipeline = services.pipeline
        conn.send(
            {
                **services.languages_message(),
                "type": "init",
                "running": pipeline.state == "running",
                "paused": pipeline.paused,
            }
        )
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "lang":
                    lang = msg.get("lang")
                    if lang not in {l["code"] for l in services.listener_languages()}:
                        conn.send({"type": "error", "error": "unknown-language"})
                        continue
                    conn.lang = lang
                    conn.send({"type": "history", "lang": lang, "segments": await pipeline.history_for(lang)})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            services.hub.listeners.discard(conn)
            conn.close()

    # -- admin API -------------------------------------------------------------

    def admin_state() -> dict:
        s = services.settings
        tr = services.translator
        source = s.source_language
        sources = sorted(
            ({"code": l["code"], "name": native_name(l["code"], l["name"])} for l in tr.languages),
            key=lambda l: l["name"].lower(),
        )
        targets = [{"code": c, "name": native_name(c, tr.name_of(c))} for c in tr.targets_for(source)]
        return {
            "settings": s.model_dump(),
            "status": services.pipeline.status(),
            "speaker_languages": [
                {"code": c, "name": native_name(c, tr.name_of(c))} for c in services.pipeline.speaker_languages()
            ],
            "vmix": services.vmix.status,
            "translator": translator_status(tr),
            "source_languages": sources,
            "target_languages": sorted(targets, key=lambda l: l["name"].lower()),
            "tts_languages": sorted(l for l in services.speech.bank_languages()),
            "models": WHISPER_MODELS,
            "addresses": lan_addresses(),
            "url": listener_url(s.public_host, s.port),
            "history": [
                {"id": seg.id, "text": seg.source, "lang": seg.lang} for seg in list(services.pipeline.history)[-30:]
            ],
        }

    @app.get("/api/admin/state")
    async def get_state(request: Request):
        require_admin(request)
        return admin_state()

    @app.get("/api/admin/devices")
    async def get_devices(request: Request):
        require_admin(request)
        return await asyncio.to_thread(list_all)

    @app.post("/api/admin/settings")
    async def save_settings(request: Request):
        require_admin(request)
        old = services.settings
        new = Settings.model_validate(await request.json())
        new.port = old.port  # the port only changes on restart (it is in the QR code)
        services.store.save(new)
        services.speech.enabled = new.tts_enabled
        if new.translation != old.translation:
            # Earlier sentences get re-translated by the new engine when someone asks for them.
            services.pipeline.clear_translations()

        async def apply_translation():
            await services.translator.apply(old, new)
            services.hub.to_listeners(services.languages_message())

        asyncio.create_task(apply_translation())
        if new.source_language != old.source_language or new.vmix.url != old.vmix.url:
            services.pipeline.speaker_language = new.source_language
            services.vmix.previous = None
        if (new.audio, new.asr, new.source_language) != (old.audio, old.asr, old.source_language):
            asyncio.create_task(services.pipeline.restart())
        services.hub.to_listeners(services.languages_message())
        return admin_state()

    @app.post("/api/admin/start")
    async def start(request: Request):
        require_admin(request)
        asyncio.create_task(services.pipeline.start())
        return {"ok": True}

    @app.post("/api/admin/stop")
    async def stop(request: Request):
        require_admin(request)
        await services.pipeline.stop()
        return {"ok": True}

    @app.post("/api/admin/pause")
    async def pause(request: Request):
        require_admin(request)
        body = await request.json()
        services.pipeline.set_paused(bool(body.get("paused")))
        return {"paused": services.pipeline.paused}

    @app.post("/api/admin/speaker")
    async def set_speaker(request: Request):
        """Switch the language being spoken at the microphone (e.g. Russian for a testimony)."""
        require_admin(request)
        lang = (await request.json()).get("lang")
        try:
            await services.pipeline.set_speaker_language(lang)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"speaker_language": services.pipeline.speaker_language, "vmix": services.vmix.status}

    @app.get("/api/admin/vmix/inputs")
    async def vmix_inputs(request: Request):
        require_admin(request)
        state = await services.vmix.inputs()
        if state is None:
            raise HTTPException(502, services.vmix.status["error"] or "vMix not reachable")
        return state

    @app.post("/api/admin/vmix/overlay")
    async def vmix_overlay(request: Request):
        require_admin(request)
        on = bool((await request.json()).get("on"))
        if not await services.vmix.overlay(on):
            raise HTTPException(502, services.vmix.status["error"] or "Choose a vMix input first")
        return services.vmix.status

    @app.post("/api/admin/vmix/test")
    async def vmix_test(request: Request):
        require_admin(request)
        text = (await request.json()).get("text") or "Live Translate ✓"
        if not await services.vmix.show(text):
            raise HTTPException(502, services.vmix.status["error"] or "Choose a vMix input and field first")
        return services.vmix.status

    @app.post("/api/admin/translator/restart")
    async def restart_translator(request: Request):
        require_admin(request)
        asyncio.create_task(services.translator.restart())
        return {"ok": True}

    @app.post("/api/admin/upload")
    async def upload(request: Request):
        """Store an audio file for rehearsals. Body is the raw file."""
        require_admin(request)
        name = re.sub(r"[^\w.\- ]", "_", request.query_params.get("name", "audio.wav"))[:100]
        folder = services.data / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with open(path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        return {"path": str(path)}

    @app.websocket("/ws/admin")
    async def admin_ws(ws: WebSocket):
        try:
            require_admin(ws)
        except HTTPException:
            await ws.close(code=4403)
            return
        await ws.accept()
        conn = Connection(ws)
        services.hub.admins.add(conn)
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            services.hub.admins.discard(conn)
            conn.close()

    return app


def translator_status(tr: TranslationService) -> dict:
    return {"engine": tr.engine, "status": tr.status, "detail": tr.detail, "needs_restart": tr.needs_restart}


async def _start_translator(services: Services) -> None:
    await services.translator.start()
    services.hub.to_listeners(services.languages_message())
    for lang in services.settings.target_languages:
        if services.settings.tts_enabled:
            await services.speech.warm_up(lang)


async def _admin_ticker(services: Services) -> None:
    gpu, tick = None, 0
    while True:
        await asyncio.sleep(0.25)
        if not services.hub.admins:
            continue
        if tick % 8 == 0:
            gpu = await asyncio.to_thread(services.gpu.read)
        tick += 1
        tr = services.translator
        services.hub.to_admins(
            {
                "type": "tick",
                "level_db": round(services.pipeline.level_db, 1),
                "status": services.pipeline.status(),
                "listeners": services.hub.counts(),
                "audio_listeners": services.speech.listener_counts(),
                "stats": services.pipeline.stats,
                "gpu": gpu,
                "translator": translator_status(tr),
                "vmix": services.vmix.status,
            }
        )
