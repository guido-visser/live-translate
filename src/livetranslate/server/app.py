"""HTTP + WebSocket server: listener pages for phones, the operator page, audio streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import autostart, calibration
from ..audio.devices import list_all
from ..config import PROFILE_FIELDS, Settings, SettingsStore, data_dir as default_data_dir
from ..integrations.vmix import VmixOutput
from ..languages import RTL, native_name, ui_language
from ..native import onnxruntime_error
from ..pipeline import WHISPER_LANGUAGES, Pipeline
from ..stt.sound import SONG_LEVELS
from ..translate.glossary import Glossary
from ..translate.service import TranslationService
from ..tts.piper import SpeechService
from ..tunnel import Tunnel
from ..updater import Updater
from .hub import Connection, Hub
from .network import GpuStats, lan_addresses, listener_url, qr_svg

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent.parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
WHISPER_MODELS = ["large-v3-turbo", "large-v3", "medium", "small", "base"]
# What phones may reach through the internet tunnel: the listener page and nothing else.
TUNNEL_PATHS = {"/", "/manifest.webmanifest", "/api/public", "/ws/listen", "/static/icon.png"}
TUNNEL_PREFIXES = ("/static/client/", "/audio/")


class FreshStaticFiles(StaticFiles):
    """Static files the browser checks before reuse, so an update never mixes new pages
    with old scripts (unchanged files still come from the cache, via their ETag)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class TunnelGuard:
    """Requests that arrive through the tunnel port only get the listener pages.

    Over the tunnel every visitor seems to come from this PC (127.0.0.1), which would
    otherwise make them operators; the port they arrive on tells them apart."""

    def __init__(self, app, tunnel: Tunnel):
        self.app = app
        self.tunnel = tunnel

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            server = scope.get("server") or (None, None)
            path = scope["path"]
            if server[1] == self.tunnel.local_port and not (path in TUNNEL_PATHS or path.startswith(TUNNEL_PREFIXES)):
                if scope["type"] == "http":
                    response = PlainTextResponse("Not available over the internet.", status_code=403)
                    await response(scope, receive, send)
                else:
                    await receive()  # websocket.connect
                    await send({"type": "websocket.close", "code": 4403})
                return
        await self.app(scope, receive, send)


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
        self.updater = Updater(data, lambda: self.pipeline.state == "stopped")
        self.tunnel = Tunnel(data, store.settings.remote.local_port)

    @property
    def settings(self) -> Settings:
        return self.store.settings

    def listener_url(self) -> str:
        """The address in the QR code: the public one when listeners come over the internet."""
        s = self.settings
        if s.remote.enabled and s.remote.public_url.strip():
            return s.remote.public_url.strip().rstrip("/") + "/"
        return listener_url(s.public_host, s.port)

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
            tasks.append(asyncio.create_task(_check_native(services)))
            tasks.append(asyncio.create_task(_start_translator(services)))
            tasks.append(asyncio.create_task(services.updater.run_checks(lambda: services.settings.check_updates)))
            if services.settings.auto_start_translation:
                tasks.append(asyncio.create_task(_auto_start(services)))
            await services.tunnel.apply(services.settings.remote)
        yield
        for task in tasks:
            task.cancel()
        await services.tunnel.stop()
        await services.pipeline.stop()
        await services.translator.close()
        await services.vmix.close()

    app = FastAPI(title="Live Translate", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(TunnelGuard, tunnel=services.tunnel)
    app.mount("/static", FreshStaticFiles(directory=WEB), name="static")

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

    @app.get("/admin/settings", include_in_schema=False)
    async def settings_page(request: Request):
        try:
            require_admin(request)
        except HTTPException as exc:
            return HTMLResponse(f"<h1>403</h1><p>{exc.detail}</p>", status_code=403)
        return FileResponse(WEB / "admin" / "settings.html", headers={"Cache-Control": "no-cache"})

    @app.get("/admin/defaults.js", include_in_schema=False)
    async def admin_defaults():
        # Loaded before common.js: the language the pages start in until the operator picks one.
        return Response(
            f"window.LT_UI_LANG = {json.dumps(ui_language())};\n",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/qr", include_in_schema=False)
    async def qr_page():
        return FileResponse(WEB / "admin" / "qr.html", headers={"Cache-Control": "no-cache"})

    # -- public API ------------------------------------------------------------

    @app.get("/api/public")
    async def public_info():
        s = services.settings
        return {
            "url": services.listener_url(),
            "wifi": s.wifi_name,
            "languages": services.listener_languages(),
        }

    @app.get("/api/qr.svg")
    async def qr():
        s = services.settings
        svg = qr_svg(services.listener_url())
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
            "quick_language_options": [
                l for l in sources if l["code"] in WHISPER_LANGUAGES and l["code"] != source
            ],
            "speaker_languages": [
                {"code": c, "name": native_name(c, tr.name_of(c))} for c in services.pipeline.speaker_languages()
            ],
            "vmix": services.vmix.status,
            "translator": translator_status(tr),
            "update": services.updater.status(),
            "tunnel": services.tunnel.status(),
            "windows": autostart.supported(),
            "logs_folder": str(services.pipeline.logs_dir),
            "profile_fields": PROFILE_FIELDS,
            "source_languages": sources,
            "target_languages": sorted(targets, key=lambda l: l["name"].lower()),
            "tts_languages": sorted(l for l in services.speech.bank_languages()),
            "models": WHISPER_MODELS,
            "addresses": lan_addresses(),
            "url": services.listener_url(),
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
        new.remote.local_port = old.remote.local_port  # bound at start
        services.store.save(new)
        if new.start_with_windows != old.start_with_windows:
            autostart.apply(new.start_with_windows)
        if new.gpu_helpers != old.gpu_helpers:
            services.speech.bank.set_gpu(new.gpu_helpers)
        services.speech.enabled = new.tts_enabled
        if new.glossary != old.glossary:
            services.translator.glossary = Glossary(new.glossary)
        if new.translation != old.translation or new.glossary != old.glossary:
            # Earlier sentences get re-translated by the new engine when someone asks for them.
            services.pipeline.clear_translations()

        async def apply_translation():
            await services.translator.apply(old, new)
            services.hub.to_listeners(services.languages_message())

        asyncio.create_task(apply_translation())
        if new.source_language != old.source_language or new.vmix.url != old.vmix.url:
            services.pipeline.speaker_language = new.source_language
            services.vmix.previous = None
        # Gain applies live; any other audio change reopens the input.
        services.pipeline.set_gain(new.audio.gain)
        audio_changed = new.audio.model_copy(update={"gain": old.audio.gain}) != old.audio
        restart_on = (new.asr, new.source_language, new.gpu_helpers, new.service_log)
        if audio_changed or restart_on != (old.asr, old.source_language, old.gpu_helpers, old.service_log):
            asyncio.create_task(services.pipeline.restart())
        if new.remote != old.remote:
            asyncio.create_task(services.tunnel.apply(new.remote))
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

    @app.get("/api/admin/profile")
    async def church_profile(request: Request):
        """This church's name, languages and glossary as a file, to move or share."""
        require_admin(request)
        data = services.settings.model_dump(include=set(PROFILE_FIELDS))
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="live-translate-church.json"'},
        )

    @app.post("/api/admin/singing")
    async def set_skip_singing(request: Request):
        """The operator's switch: skip songs automatically, or translate everything."""
        require_admin(request)
        on = bool((await request.json()).get("on"))
        services.store.save(services.settings.model_copy(update={"skip_singing": on}))
        return services.pipeline.status()

    @app.post("/api/admin/singing/level")
    async def set_song_level(request: Request):
        """The calibration slider; the recognition thread picks this up at its next pass."""
        require_admin(request)
        level = (await request.json()).get("level")
        if level not in SONG_LEVELS:
            raise HTTPException(400, "Unknown level")
        services.store.save(services.settings.model_copy(update={"song_level": level}))
        return {"level": level}

    def running_log() -> Path | None:
        current = services.pipeline.service_log
        return current.folder if current else None

    def finished_log(name: str) -> Path:
        root = services.pipeline.logs_dir
        folder = root / name
        if folder.parent != root or folder == running_log() or not (folder / "events.jsonl").is_file():
            raise HTTPException(404, "No such service")
        return folder

    @app.get("/api/admin/logs")
    async def list_logs(request: Request):
        require_admin(request)
        root, running = services.pipeline.logs_dir, running_log()
        return await asyncio.to_thread(calibration.list_services, root, running)

    @app.get("/api/admin/logs/{name}/analysis")
    async def analyse_log(name: str, request: Request):
        require_admin(request)
        return await asyncio.to_thread(calibration.analyse, finished_log(name))

    @app.post("/api/admin/logs/{name}/fit")
    async def fit_log(name: str, request: Request):
        """The slider level that suits the stretches the operator labelled as music or speech."""
        require_admin(request)
        folder, labels = finished_log(name), (await request.json())["labels"]
        analysis = await asyncio.to_thread(calibration.analyse, folder)
        return {"level": calibration.best_level(analysis, labels)}

    @app.get("/api/admin/logs/{name}/clip")
    async def log_clip(name: str, t: float, request: Request):
        require_admin(request)
        folder = finished_log(name)
        if not (folder / "audio.flac").is_file():
            raise HTTPException(404, "This service has no recording")
        return Response(await asyncio.to_thread(calibration.audio_clip, folder, t), media_type="audio/wav")

    @app.post("/api/admin/mark")
    async def mark(request: Request):
        """Note this moment in the service log ("this went wrong")."""
        require_admin(request)
        services.pipeline.mark()
        return {"ok": True}

    @app.post("/api/admin/logs/open")
    async def open_logs(request: Request):
        require_admin(request)
        folder = services.pipeline.logs_dir
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)
        return {"folder": str(folder)}

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

    @app.post("/api/admin/glossary/test")
    async def glossary_test(request: Request):
        """Translate a sample sentence with the saved glossary, to check the words."""
        require_admin(request)
        text = str((await request.json()).get("text") or "").strip()[:500]
        s = services.settings
        langs = [l for l in s.target_languages if l != s.source_language]
        if not text or not langs:
            return {}
        # Sentence by sentence, as during a service (NLLB tends to drop a short first sentence otherwise).
        sentences = [p for p in re.split(r"(?<=[.!?…])\s+", text) if p]

        async def translate(lang: str) -> dict:
            parts = [await services.translator.translate(p, s.source_language, lang) for p in sentences]
            result = {"text": " ".join(parts)}
            if lang != "en":
                # Back into English, so an operator who can't read Russian can check the meaning.
                back = [await services.translator.translate(p, lang, "en") for p in parts]
                result["back"] = " ".join(back)
            return result

        try:
            results = await asyncio.gather(*(translate(lang) for lang in langs))
        except Exception as exc:
            raise HTTPException(503, f"Translation is not available: {exc}") from exc
        return dict(zip(langs, results))

    @app.post("/api/admin/translator/restart")
    async def restart_translator(request: Request):
        require_admin(request)
        asyncio.create_task(services.translator.restart())
        return {"ok": True}

    @app.post("/api/admin/update/check")
    async def check_update(request: Request):
        require_admin(request)
        return await services.updater.check()

    @app.post("/api/admin/update/install")
    async def install_update(request: Request):
        """Download and start the new installer; the app then closes and Setup starts it again."""
        require_admin(request)
        try:
            await services.updater.install()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return services.updater.status()

    @app.post("/api/admin/tunnel/key")
    async def tunnel_key(request: Request):
        """Create this PC's key pair for the tunnel (once); returns the tunnel status with the public key."""
        require_admin(request)
        try:
            await services.tunnel.create_key()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return services.tunnel.status()

    @app.post("/api/admin/tunnel/forget")
    async def tunnel_forget(request: Request):
        """Forget the server's identity (after reinstalling it) and connect again."""
        require_admin(request)
        services.tunnel.forget_server()
        remote = services.settings.remote
        await services.tunnel.stop()
        await services.tunnel.apply(remote)
        return services.tunnel.status()

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


async def _check_native(services: Services) -> None:
    """Find out early whether onnxruntime loads, so the operator page explains a broken PC."""
    error = await asyncio.to_thread(onnxruntime_error)
    if error and services.pipeline.state == "stopped" and not services.pipeline.error:
        services.pipeline.error = error
        services.hub.to_admins({"type": "status", **services.pipeline.status()})
        services.hub.to_listeners(services.languages_message())  # spoken translations are gone


async def _auto_start(services: Services, tries: int = 30, wait_s: float = 10) -> None:
    """Start translating by itself. Right after boot a USB audio interface may not be there yet."""
    for attempt in range(tries):
        await services.pipeline.start()
        if services.pipeline.state != "stopped":
            return
        log.info("Automatic start failed (%d of %d): %s", attempt + 1, tries, services.pipeline.error)
        await asyncio.sleep(wait_s)


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
                "update": services.updater.status(),
                "tunnel": services.tunnel.status(),
            }
        )
