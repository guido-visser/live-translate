"""HTTP + WebSocket server: listener pages for phones, the operator page, audio streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .. import __version__, autostart, calibration, firewall
from ..audio.devices import list_all
from ..config import (
    PROFILE_FIELDS,
    WHISPER_MODELS,
    Settings,
    SettingsStore,
    SongModel,
    data_dir as default_data_dir,
    is_network_path,
)
from ..firstrun import Preparation
from ..integrations.obs import ObsOutput
from ..integrations.vmix import VmixOutput
from ..languages import RTL, native_name, ui_language
from ..native import onnxruntime_error
from ..pipeline import WHISPER_LANGUAGES, Pipeline
from ..stt.sound import SONG_LEVELS
from ..training import Trainer, is_youtube, list_youtube
from ..translate.glossary import Glossary
from ..translate.service import TranslationService
from ..tts.piper import VOICES, SpeechService
from ..tunnel import Tunnel
from ..updater import Updater, install_dir, start_data_move
from .auth import COOKIE, MIN_PASSWORD, SESSION_S, LoginLimiter, SessionStore, check_password, hash_password
from .files import WebFiles
from .hub import Connection, Hub
from .network import GpuStats, host_url, lan_addresses, lan_networks, listener_url, qr_svg, subnet

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent.parent / "web"
# A request from this PC itself: its address, and the name the browser used for it. Only names a
# browser always resolves to this PC: a name looked up on the network could be made to point here
# (DNS rebinding) by a website, which would then count as this PC.
LOCAL_HOSTS = {"127.0.0.1", "::1"}
LOCAL_NAMES = {"127.0.0.1", "::1", "localhost"}
# Phones: a full church fits easily; one device can't use up the PC with hundreds of connections.
MAX_LISTENERS = 200
MAX_LISTENERS_PER_DEVICE = 10
# Requests are small (the settings with a trained model are ~50 KB); uploads have their own route.
MAX_BODY = 1_000_000
OWN_NAMES_S = 30
SAFE_LOG_NAME = re.compile(r"[\w\- ][\w.\- ]*")
# What phones may reach through the internet tunnel: the listener page and nothing else.
TUNNEL_PATHS = {"/", "/manifest.webmanifest", "/sw.js", "/api/public", "/ws/listen", "/static/icon.png"}
TUNNEL_PREFIXES = ("/static/client/", "/audio/")


def _safe_next(target: str | None) -> str:
    """Where the login page goes afterwards: only a page of the admin itself, never another site."""
    target = target or ""
    ours = target.startswith("/admin") or target == "/qr" or target.startswith("/qr?")
    if ours and not target.startswith("/admin/login") and "//" not in target and "\\" not in target:
        return target
    return "/admin"


def _host_name(address: str) -> str:
    try:
        return (urlsplit(f"//{address}").hostname or "").lower()
    except ValueError:
        return ""


class LimitBody:
    """Refuses request bodies over MAX_BODY, also when they don't say their size up front: one
    request could otherwise fill the memory. The upload route (only on this PC) streams to disk."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/api/admin/upload":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > MAX_BODY:
            await PlainTextResponse("Request too large.", status_code=413)(scope, receive, send)
            return
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY:
                    raise HTTPException(413, "Request too large.")
            return message

        await self.app(scope, limited, send)


class SecurityHeaders:
    """What browsers need to keep pages of this app from being misused: never inside another
    website's frame (clickjacking), only this app's own scripts (no injected ones), no guessing of
    file types, no address of the admin page leaking to links it opens, admin data never cached."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
        host = host if re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", host) else ""
        # 'self' covers websockets in current browsers; older Safari needs them spelled out.
        sockets = f" ws://{host} wss://{host}" if host else ""
        # The app window (pywebview) builds its bridge to Python with new Function(), which needs
        # 'unsafe-eval'. Only for pages opened on this PC itself; injected scripts stay refused.
        client = (scope.get("client") or ("", 0))[0]
        on_this_pc = client in LOCAL_HOSTS and _is_local_name(host)
        scripts = "'self' 'unsafe-eval'" if on_this_pc else "'self'"
        policy = (
            f"default-src 'self'; script-src {scripts}; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; "
            f"connect-src 'self'{sockets}; worker-src 'self'; manifest-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        path = scope["path"]
        private = path.startswith(("/api/admin", "/admin", "/api/qr", "/qr"))

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                extra = {
                    b"content-security-policy": policy.encode(),
                    b"x-frame-options": b"DENY",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"cross-origin-resource-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                }
                if private and path.startswith(("/api/admin", "/api/qr")):
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    present.discard(b"cache-control")
                    extra[b"cache-control"] = b"no-store"
                headers += [(k, v) for k, v in extra.items() if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


def _port_free(port: int) -> bool:
    try:
        socket.create_server(("0.0.0.0", port)).close()
    except OSError:
        return False
    return True


def _is_local_name(address: str) -> bool:
    """The Host header of a request: "127.0.0.1:8000", "[::1]:8000", "localhost:8000"."""
    return _host_name(address) in LOCAL_NAMES


class LogFailedRequests:
    """Uvicorn logs the traceback of a failed request but not which address it was for."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception:
            if scope["type"] in ("http", "websocket"):
                log.error("Request failed: %s %s", scope.get("method", "WEBSOCKET"), scope.get("path"))
            raise


def _quiet_disconnects(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """A phone that leaves mid-stream makes Windows' asyncio log a ConnectionResetError traceback
    (in _ProactorBasePipeTransport._call_connection_lost). That is normal; everything else still counts."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        log.debug("A connection closed: %s", context.get("message"))
        return
    loop.default_exception_handler(context)


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
        self.obs = ObsOutput(lambda: store.settings)
        self.pipeline = Pipeline(store, self.translator, self.speech, self.hub, data, self.vmix, self.obs)
        self.gpu = GpuStats()
        self.updater = Updater(data, lambda: self.pipeline.state == "stopped")
        self.tunnel = Tunnel(data, store.settings.remote.local_port)
        # The port this server listens on; settings.port may be newer, until the app restarts.
        self.port = store.settings.port
        self.sessions = SessionStore(data / "sessions.json")
        self.limiter = LoginLimiter()
        self.login_delay_s = 1.0  # after a wrong password
        self.restart = None  # set by __main__: start the app again (for a new port)
        self.firewall_problem = False  # the firewall rule opens another port than this server's
        self.firewall_change = ""  # how the last change of the rule went: ok, cancelled, failed
        self.listener_devices: Counter[str] = Counter()  # open listener connections per address
        self._own_names: tuple[float, set[str]] = (0.0, set())
        self.preparation = Preparation(self)
        self.trainer = Trainer(
            data,
            lambda: store.settings,
            lambda: self.pipeline.state == "stopped",
            self.use_song_model,
            lambda: store.settings.gpu_helpers,
        )

    @property
    def settings(self) -> Settings:
        return self.store.settings

    def use_song_model(self, model: SongModel | None) -> None:
        self.store.save(self.settings.model_copy(update={"song_model": model}))

    def listener_url(self) -> str:
        """The address in the QR code: the public one when listeners come over the internet."""
        s = self.settings
        if s.remote.enabled and s.remote.public_url.strip():
            return s.remote.public_url.strip().rstrip("/") + "/"
        return listener_url(s.public_host, self.port)

    def listener_urls(self) -> list[dict]:
        """Every address listeners can use, one tab each with the QR code: the internet address,
        a typed-in address, and one per network of this PC. The first is listener_url().

        A network's tab is named after its Wi-Fi: the name typed in for it, or the SSID when this PC
        is on that Wi-Fi itself; otherwise after the adapter ("Ethernet 2"). "key" is what a typed-in
        Wi-Fi name is saved under; "wifi" is the name shown under the code."""
        s = self.settings
        urls: list[dict] = []
        if s.remote.enabled and s.remote.public_url.strip():
            urls.append({"name": "Internet", "detail": "", "url": self.listener_url(), "key": "", "wifi": ""})
        networks = lan_networks()
        host = s.public_host.strip()
        if host and host not in {n["ip"] for n in networks}:
            urls.append({"name": host, "detail": "", "url": host_url(host, self.port), "key": "", "wifi": ""})
        for n in networks:
            key = subnet(n["ip"])
            wifi = s.wifi_names.get(key, "").strip() or n["ssid"]
            adapter = " · ".join(x for x in (n["name"], n["ip"]) if x)
            urls.append({
                "name": wifi or n["name"] or n["ip"],
                "detail": adapter if wifi else (n["ip"] if n["name"] else ""),
                "url": host_url(n["ip"], self.port),
                "key": key,
                "wifi": wifi,
            })
        default = self.listener_url()
        urls.sort(key=lambda u: u["url"] != default)  # stable: only moves the default to the front
        if not urls or urls[0]["url"] != default:
            urls.insert(0, {"name": default, "detail": "", "url": default, "key": "", "wifi": ""})
        if not urls[0]["wifi"]:
            urls[0]["wifi"] = s.wifi_name.strip()  # the one Wi-Fi name from before there were tabs
        return urls

    def own_names(self) -> set[str]:
        """The names other devices may use for this PC to reach the admin page: its addresses on
        each network, its computer name, and the address typed in for the QR code."""
        at, names = self._own_names
        if time.monotonic() - at < OWN_NAMES_S and names:
            return names
        names = {n["ip"] for n in lan_networks()} | set(lan_addresses())
        computer = socket.gethostname().lower()
        names |= {computer, f"{computer}.local"}
        typed = self.settings.public_host.strip()
        if typed:
            names.add(_host_name(typed.split("://", 1)[-1].split("/", 1)[0]))
        names.discard("")
        self._own_names = (time.monotonic(), names)
        return names

    def admin_urls(self) -> list[dict]:
        """The admin page for other devices, one per network of this PC (named like listener_urls)."""
        s = self.settings
        urls = []
        for n in lan_networks():
            wifi = s.wifi_names.get(subnet(n["ip"]), "").strip() or n["ssid"]
            urls.append({
                "name": wifi or n["name"] or n["ip"],
                "detail": " · ".join(x for x in (n["name"], n["ip"]) if x),
                "url": host_url(n["ip"], self.port) + "admin",
            })
        return urls

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


def create_app(
    store: SettingsStore | None = None,
    data: Path | None = None,
    manage_translator: bool = True,
    resume: bool = False,
) -> FastAPI:
    """resume: start translating as soon as possible, also without auto_start_translation (the
    app restarted itself while it was translating)."""
    store = store or SettingsStore()
    services = Services(store, data or default_data_dir())
    web = WebFiles(WEB)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if sys.platform == "win32":
            asyncio.get_running_loop().set_exception_handler(_quiet_disconnects)
        services.pipeline.ensure_worker()
        tasks = [asyncio.create_task(_admin_ticker(services))]
        if manage_translator:
            tasks.append(asyncio.create_task(_check_native(services)))
            tasks.append(asyncio.create_task(services.updater.run_checks(lambda: services.settings.check_updates)))
            if services.settings.setup_done:  # otherwise the wizard starts these when it has the choices
                tasks.append(asyncio.create_task(_start_translator(services)))
                if services.settings.auto_start_translation or resume:
                    tasks.append(asyncio.create_task(_auto_start(services)))
            await services.tunnel.apply(services.settings.remote)
            tasks.append(asyncio.create_task(_check_firewall(services)))
        yield
        for task in tasks:
            task.cancel()
        await services.tunnel.stop()
        await services.pipeline.stop()
        await services.translator.close()
        await services.vmix.close()
        await services.obs.close()

    app = FastAPI(title="Live Translate", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(TunnelGuard, tunnel=services.tunnel)
    app.add_middleware(LimitBody)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(LogFailedRequests)

    def client_ip(request: Request | WebSocket) -> str:
        return request.client.host if request.client else ""

    def check_origin(request: Request | WebSocket) -> None:
        # A website open in a browser on this PC also comes from 127.0.0.1. Browsers say where a
        # request comes from (Origin); the address checks out too when a website's name points here.
        address = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].lower() != address.lower():
            raise HTTPException(403, "Requests from other websites are refused.")

    def is_local(request: Request | WebSocket) -> bool:
        return client_ip(request) in LOCAL_HOSTS and _is_local_name(request.headers.get("host", ""))

    def remote_admin_on() -> bool:
        """Other devices may sign in. Never while listening over the internet is on: then the
        admin page stays on this PC, however the tunnel's server is set up."""
        s = services.settings
        return s.web_server.lan_admin and bool(s.web_server.password_hash) and not s.remote.enabled

    def check_own_address(request: Request | WebSocket) -> None:
        """Another device must open the admin page by this PC's own address or name, not by some
        other name that was made to point here (DNS rebinding)."""
        if _host_name(request.headers.get("host", "")) not in services.own_names():
            raise HTTPException(403, "Open the admin page by this computer's own address.")

    def admin_access(request: Request | WebSocket) -> str:
        """"local" on this PC; "remote" for another device that signed in (Settings → Web Server).
        401: it may sign in; 403: the admin page isn't open to other devices."""
        check_origin(request)
        if is_local(request):
            return "local"
        if remote_admin_on():
            check_own_address(request)
            session_id = services.sessions.check(request.cookies.get(COOKIE), client_ip(request))
            if session_id:
                request.state.session_id = session_id
                return "remote"
            raise HTTPException(401, "Sign in first.")
        raise HTTPException(403, "The admin page is only available on this computer.")

    def require_admin(request: Request | WebSocket) -> str:
        return admin_access(request)

    def require_local_admin(request: Request | WebSocket, detail: str = "This can only be done on the PC itself.") -> None:
        if admin_access(request) != "local":
            raise HTTPException(403, detail)

    def admin_page(request: Request) -> Response | None:
        """None when this device may see the admin pages; otherwise the login page or a 403."""
        try:
            require_admin(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse(f"/admin/login?next={quote(request.url.path)}", status_code=303)
            return HTMLResponse(f"<h1>403</h1><p>{exc.detail}</p>", status_code=403)
        return None

    async def close_admin_ws(conn: Connection) -> None:
        with contextlib.suppress(Exception):
            await conn.ws.close(code=4401)

    def end_sessions(session_id: str | None = None) -> None:
        """Sign out one device (or all of them); their open admin pages go to the login page."""
        if session_id:
            services.sessions.revoke(session_id)
        else:
            services.sessions.revoke_all()
        for conn in list(services.hub.admins):
            sid = getattr(conn, "session_id", None)
            if sid and (session_id is None or sid == session_id):
                asyncio.create_task(close_admin_ws(conn))

    # -- pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def listener_page(request: Request):
        return web.response("client/index.html", request)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest(request: Request):
        return web.response("client/manifest.webmanifest", request)

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker(request: Request):
        # At the top, not under /static/: a service worker only covers pages below its own address.
        return web.response("client/sw.js", request)

    @app.get("/admin", include_in_schema=False)
    async def main_admin_page(request: Request):
        if (refused := admin_page(request)) is not None:
            return refused
        if not services.settings.setup_done:
            query = request.url.query
            return RedirectResponse(f"/admin/setup?{query}" if query else "/admin/setup", status_code=307)
        return web.response("admin/index.html", request)

    @app.get("/admin/setup", include_in_schema=False)
    async def setup_page(request: Request):
        if (refused := admin_page(request)) is not None:
            return refused
        return web.response("admin/setup.html", request)

    @app.get("/admin/settings", include_in_schema=False)
    async def settings_page(request: Request):
        if (refused := admin_page(request)) is not None:
            return refused
        return web.response("admin/settings.html", request)

    @app.get("/admin/login", include_in_schema=False)
    async def login_page(request: Request):
        if is_local(request):
            return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
        if not remote_admin_on():
            return HTMLResponse("<h1>403</h1><p>The admin page is only available on this computer.</p>", status_code=403)
        try:
            check_own_address(request)
        except HTTPException as exc:
            return HTMLResponse(f"<h1>403</h1><p>{exc.detail}</p>", status_code=403)
        return web.response("admin/login.html", request)

    def set_session_cookie(response: Response, token: str) -> None:
        # Not Secure: the page is plain HTTP on the local network (Settings → Web Server says so).
        response.set_cookie(COOKIE, token, max_age=SESSION_S, httponly=True, samesite="strict", path="/")

    @app.post("/api/admin/login")
    async def login(request: Request):
        check_origin(request)
        if is_local(request):
            return {"ok": True}  # this PC needs no password
        if not remote_admin_on():
            raise HTTPException(403, "The admin page is only available on this computer.")
        check_own_address(request)
        ip = client_ip(request)

        def refuse_if_blocked() -> None:
            wait = services.limiter.blocked_for(ip)
            if wait:
                raise HTTPException(429, "Too many wrong passwords.", headers={"Retry-After": str(wait)})

        refuse_if_blocked()
        try:
            password = str((await request.json()).get("password") or "")[:200]
        except ValueError as exc:
            raise HTTPException(400, "No password") from exc
        if not await asyncio.to_thread(check_password, password, services.settings.web_server.password_hash):
            services.limiter.failed(ip)
            log.info("Wrong admin password from %s", ip)
            await asyncio.sleep(services.login_delay_s)
            refuse_if_blocked()
            raise HTTPException(401, "Wrong password.")
        services.limiter.succeeded(ip)
        token, _ = services.sessions.create(ip, request.headers.get("user-agent", ""))
        log.info("Admin page signed in from %s", ip)
        response = JSONResponse({"ok": True})
        set_session_cookie(response, token)
        return response

    @app.post("/api/admin/logout")
    async def logout(request: Request):
        check_origin(request)
        session_id = services.sessions.check(request.cookies.get(COOKIE))
        if session_id:
            end_sessions(session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/admin/defaults.js", include_in_schema=False)
    async def admin_defaults():
        # Loaded before common.js: the language the pages start in until the operator picks one.
        return Response(
            f"window.LT_UI_LANG = {json.dumps(ui_language())};\n",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/qr", include_in_schema=False)
    async def qr_page(request: Request):
        # It lists this PC's networks and addresses: for the operator, not for everyone on the Wi-Fi.
        if (refused := admin_page(request)) is not None:
            return refused
        return web.response("admin/qr.html", request)

    @app.api_route("/static/{name:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def static_file(name: str, request: Request):
        return web.response(name, request)

    @app.get("/api/health", include_in_schema=False)
    async def health():
        """For the app's own check (see health.py): a page and both kinds of worker-thread hop."""
        await anyio.to_thread.run_sync(int)  # what FileResponse and plain def endpoints use
        await asyncio.to_thread(int)
        return {"ok": web.get("admin/qr.html") is not None}

    # -- public API ------------------------------------------------------------

    @app.get("/api/public")
    async def public_info():
        s = services.settings
        return {
            "url": services.listener_url(),
            "wifi": s.wifi_name,
            "church": s.church_name,
            "languages": services.listener_languages(),
        }

    @app.get("/api/qr/addresses")
    async def qr_addresses(request: Request):
        """The tabs with the QR code: they list this PC's addresses, so only for admins."""
        require_admin(request)
        return {"addresses": await asyncio.to_thread(services.listener_urls)}

    @app.post("/api/admin/wifi-name")
    async def set_wifi_name(request: Request):
        """The Wi-Fi name of one network (by its "key" from /api/qr/addresses); empty forgets it."""
        require_admin(request)
        body = await request.json()
        key, name = str(body.get("key", "")).strip(), str(body.get("name", "")).strip()[:64]
        if not key:
            raise HTTPException(400, "No network")
        names = {k: v for k, v in services.settings.wifi_names.items() if k != key}
        if name:
            names[key] = name
        services.store.save(services.settings.model_copy(update={"wifi_names": names}))
        return {"addresses": await asyncio.to_thread(services.listener_urls)}

    @app.get("/api/qr.svg")
    async def qr(request: Request, url: str = ""):
        require_admin(request)
        # Only addresses of this app, so the QR page can't be made to point anywhere else.
        if not url or url == services.listener_url():
            url = services.listener_url()
        elif url not in {u["url"] for u in await asyncio.to_thread(services.listener_urls)} and not (
            remote_admin_on() and url in {u["url"] for u in await asyncio.to_thread(services.admin_urls)}
        ):
            raise HTTPException(404, "Unknown address")
        svg = qr_svg(url)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    def listener_device(conn: Request | WebSocket) -> str:
        """Who counts as one device. Over the tunnel everyone seems to be this PC, so there the
        connection's own port tells them apart (only the total limit applies)."""
        server = conn.scope.get("server") or (None, None)
        client = conn.scope.get("client") or ("", 0)
        if server[1] and server[1] == services.tunnel.local_port:
            return f"tunnel:{client[1]}"
        return client_ip(conn)

    def listener_room(device: str) -> bool:
        devices = services.listener_devices
        return sum(devices.values()) < MAX_LISTENERS and devices[device] < MAX_LISTENERS_PER_DEVICE

    def listener_join(device: str) -> None:
        services.listener_devices[device] += 1

    def listener_leave(device: str) -> None:
        devices = services.listener_devices
        devices[device] -= 1
        if devices[device] <= 0:
            del devices[device]

    @app.get("/audio/{lang}.mp3")
    async def audio_stream(lang: str, request: Request):
        if not services.speech.has_voice(lang):
            raise HTTPException(404, "No voice for this language")
        if lang not in {l["code"] for l in services.listener_languages()}:
            raise HTTPException(404, "Language not offered")
        device = listener_device(request)
        if not listener_room(device):
            raise HTTPException(503, "Too many connections.")
        asyncio.create_task(services.speech.warm_up(lang))
        stream = services.speech.stream(lang)

        async def body():
            # Counted from the first byte: a request that never gets that far holds nothing.
            listener_join(device)
            queue = stream.subscribe()
            services.pipeline.event("audio_listener", lang=lang, listeners=len(stream.clients))
            try:
                while True:
                    yield await queue.get()
            finally:
                stream.unsubscribe(queue)
                listener_leave(device)
                services.pipeline.event("audio_listener", lang=lang, listeners=len(stream.clients))

        headers = {"Cache-Control": "no-cache, no-store", "X-Content-Type-Options": "nosniff"}
        return StreamingResponse(body(), media_type="audio/mpeg", headers=headers)

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket):
        device = listener_device(ws)
        if not listener_room(device):
            await ws.close(code=1013)  # try again later
            return
        listener_join(device)
        try:
            await listen_on(ws)
        finally:
            listener_leave(device)

    async def listen_on(ws: WebSocket):
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

    def settings_for(access: str) -> dict:
        """The settings as a page gets them: never the password hash; on another device not the
        OBS password either (it is kept as it is when that device saves)."""
        data = services.settings.model_dump(exclude={"web_server": {"password_hash"}})
        if access == "remote":
            data["obs"]["password"] = ""
        return data

    def admin_state(access: str = "local") -> dict:
        s = services.settings
        tr = services.translator
        source = s.source_language
        sources = sorted(
            ({"code": c, "name": native_name(c, tr.name_of(c))} for c in tr.catalog),
            key=lambda l: l["name"].lower(),
        )
        targets = [l for l in sources if l["code"] != source]
        return {
            # For the wizard, which offers a choice of engine before either has been started.
            "catalogs": {"libretranslate": tr.libre.catalog, "nllb": tr.nllb.catalog},
            "whisper_languages": sorted(WHISPER_LANGUAGES),
            "voices": sorted(VOICES),
            "version": __version__,
            "data_folder": str(services.data),
            "can_move_data": install_dir() is not None,
            "settings": settings_for(access),
            "access": access,
            "firewall_problem": services.firewall_problem,
            "status": services.pipeline.status(),
            "quick_language_options": [
                l for l in sources if l["code"] in WHISPER_LANGUAGES and l["code"] != source
            ],
            "speaker_languages": [
                {"code": c, "name": native_name(c, tr.name_of(c))} for c in services.pipeline.speaker_languages()
            ],
            "vmix": services.vmix.status,
            "obs": services.obs.status,
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
        access = require_admin(request)
        response = JSONResponse(admin_state(access))
        token = request.cookies.get(COOKIE)
        if access == "remote" and token:
            set_session_cookie(response, token)  # the cookie lasts as long as the session: 30 days from now
        return response

    @app.get("/api/admin/devices")
    async def get_devices(request: Request):
        require_admin(request)
        return await asyncio.to_thread(list_all)

    # The settings page saves by itself, so saves can follow each other closely.
    translation_turn, pipeline_turn, tunnel_turn = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

    def in_turn(lock: asyncio.Lock, job) -> None:
        async def run():
            async with lock:
                await job()

        asyncio.create_task(run())

    @app.post("/api/admin/settings")
    async def save_settings(request: Request):
        access = require_admin(request)
        old = services.settings
        body = await request.json()
        body.pop("web_server", None)  # without the password hash, which pages never get
        new = Settings.model_validate(body)
        new.web_server = old.web_server  # changed in its own tab, only on this PC (/api/admin/webserver/*)
        if access == "remote":
            # What belongs to this PC: the tunnel goes with its key, a file path is read by this
            # PC's account, the translation server gets every sentence, the OBS password is hidden.
            new.remote = old.remote
            new.audio.file_path = old.audio.file_path
            new.libretranslate_url, new.libretranslate_port = old.libretranslate_url, old.libretranslate_port
            new.obs.password = old.obs.password
        if is_network_path(new.audio.file_path):
            raise HTTPException(400, "Files on a network share can't be used; copy the file to this PC.")
        new.port = old.port  # the port only changes on restart (it is in the QR code)
        new.remote.local_port = old.remote.local_port  # bound at start
        # These have their own switch and slider; a page's older copy must not undo them.
        new.skip_singing, new.song_level = old.skip_singing, old.song_level
        new.song_model = old.song_model  # so does training, and a page mustn't undo that either
        new.wifi_names = old.wifi_names  # typed in with the QR code
        services.store.save(new)
        if new.remote.enabled and not old.remote.enabled:
            end_sessions()  # listening over the internet: the admin page stays on this PC
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

        in_turn(translation_turn, apply_translation)
        if new.source_language != old.source_language or new.vmix.url != old.vmix.url:
            services.pipeline.speaker_language = new.source_language
            services.vmix.previous = None
        if new.source_language != old.source_language or new.obs.source != old.obs.source:
            services.obs.previous = None
        # Gain applies live; any other audio change reopens the input.
        services.pipeline.set_gain(new.audio.gain)
        audio_changed = new.audio.model_copy(update={"gain": old.audio.gain}) != old.audio
        restart_on = (new.asr, new.source_language, new.gpu_helpers, new.service_log)
        if audio_changed or restart_on != (old.asr, old.source_language, old.gpu_helpers, old.service_log):
            in_turn(pipeline_turn, services.pipeline.restart)
        if new.remote != old.remote:
            in_turn(tunnel_turn, lambda: services.tunnel.apply(new.remote))
        services.hub.to_listeners(services.languages_message())
        return admin_state(access)

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

    @app.post("/api/admin/clear")
    async def clear_transcript(request: Request):
        require_admin(request)
        await services.pipeline.clear_transcript()
        return {"ok": True}

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
        """A service's folder, by its name: only a folder directly in the logs folder."""
        root = services.pipeline.logs_dir
        if not SAFE_LOG_NAME.fullmatch(name) or name.strip(". ") != name.strip():
            raise HTTPException(404, "No such service")
        folder = root / name
        if folder.resolve().parent != root.resolve() or folder == running_log() or not (folder / "events.jsonl").is_file():
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

    @app.get("/api/admin/training")
    async def training_status(request: Request):
        require_admin(request)
        return services.trainer.status()

    @app.post("/api/admin/training/add")
    async def training_add(request: Request):
        """Lines with YouTube links or file paths. Channels and playlists come back, to choose from.
        File paths only on this PC: they are read with this PC's account."""
        access = require_admin(request)
        return services.trainer.add(str((await request.json()).get("text", "")), allow_files=access == "local")

    @app.post("/api/admin/training/list")
    async def training_list(request: Request):
        """What is in a channel or playlist; nothing is downloaded yet."""
        require_admin(request)
        url = str((await request.json()).get("url", ""))
        if not is_youtube(url):
            raise HTTPException(400, "Not a YouTube link")
        try:
            return {"videos": await asyncio.to_thread(list_youtube, url)}
        except Exception as exc:
            log.warning("Couldn't list %s: %s", url, exc)
            raise HTTPException(502, "Couldn't read this link") from exc

    @app.post("/api/admin/training/add-listed")
    async def training_add_listed(request: Request):
        require_admin(request)
        return {"added": services.trainer.add_listed(list((await request.json()).get("videos", [])))}

    @app.delete("/api/admin/training/sources/{source_id}")
    async def training_remove(source_id: str, request: Request):
        require_admin(request)
        services.trainer.remove(source_id)
        return services.trainer.status()

    @app.post("/api/admin/training/start")
    async def training_start(request: Request):
        require_admin(request)
        if not services.trainer.start():
            raise HTTPException(409, "Training can't start now")
        return services.trainer.status()

    @app.post("/api/admin/training/stop")
    async def training_stop(request: Request):
        require_admin(request)
        services.trainer.stop()
        return services.trainer.status()

    @app.post("/api/admin/training/model")
    async def training_model(request: Request):
        """Go back to the standard detection (null), or take a model from a church profile."""
        require_admin(request)
        body = (await request.json()).get("model")
        try:
            services.use_song_model(None if body is None else SongModel.model_validate(body))
        except ValueError as exc:
            raise HTTPException(400, "Not a trained model") from exc
        return admin_state()

    @app.post("/api/admin/setup/prepare")
    async def prepare(request: Request):
        """Download what the chosen settings need; the wizard follows it with /setup/status."""
        require_admin(request)
        services.preparation.start()
        return services.preparation.status()

    @app.get("/api/admin/setup/status")
    async def preparation_status(request: Request):
        require_admin(request)
        return await asyncio.to_thread(services.preparation.status)

    @app.post("/api/admin/setup/finish")
    async def finish_setup(request: Request):
        require_admin(request)
        services.store.save(services.settings.model_copy(update={"setup_done": True}))
        if services.translator.status in ("stopped", "error"):  # the wizard was finished despite a failed step
            asyncio.create_task(_start_translator(services))
        if services.settings.auto_start_translation:
            asyncio.create_task(_auto_start(services))
        return {"ok": True}

    @app.post("/api/admin/setup/again")
    async def setup_again(request: Request):
        """Settings → About: go through the wizard again."""
        require_admin(request)
        services.store.save(services.settings.model_copy(update={"setup_done": False}))
        services.preparation.forget()
        return {"ok": True}

    @app.get("/api/admin/close")
    async def close_action(request: Request):
        """The cross of the app window: what to do. See LT.askClose in common.js."""
        require_admin(request)
        return {"action": services.settings.close_action}

    @app.post("/api/admin/close")
    async def close_choice(request: Request):
        require_local_admin(request, "Only the window on the PC itself can do this.")
        body = await request.json()
        action = body.get("action")
        if action not in ("tray", "exit"):
            raise HTTPException(400, "Unknown action.")
        if body.get("remember"):
            services.store.save(services.settings.model_copy(update={"close_action": action}))
        if action == "exit" and services.updater.request_exit:
            log.info("Exit chosen when closing the window")
            asyncio.get_running_loop().call_later(0.5, services.updater.request_exit)
        return {"ok": True}

    @app.post("/api/admin/data/move")
    async def move_data(request: Request):
        """Close the app and let the setup window move the data; see LiveTranslateSetup.ps1."""
        require_local_admin(request, "The folder is chosen on the PC itself.")
        folder = install_dir()
        if folder is None:
            raise HTTPException(400, "Only the installed app can move its data.")
        if services.pipeline.state != "stopped":
            raise HTTPException(409, "Stop translating first.")
        start_data_move(folder)
        asyncio.get_running_loop().call_later(1.0, services.updater.request_exit)
        return {"ok": True}

    @app.post("/api/admin/mark")
    async def mark(request: Request):
        """Note this moment in the service log ("this went wrong")."""
        require_admin(request)
        services.pipeline.mark()
        return {"ok": True}

    @app.post("/api/admin/logs/open")
    async def open_logs(request: Request):
        require_local_admin(request)
        folder = services.pipeline.logs_dir
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)
        return {"folder": str(folder)}

    @app.post("/api/admin/speaker")
    async def set_speaker(request: Request):
        """Switch the language being spoken at the microphone (e.g. for a testimony in another language)."""
        require_admin(request)
        lang = (await request.json()).get("lang")
        try:
            await services.pipeline.set_speaker_language(lang)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "speaker_language": services.pipeline.speaker_language,
            "vmix": services.vmix.status,
            "obs": services.obs.status,
        }

    def streaming_routes(name: str, output) -> None:
        async def inputs(request: Request):
            require_admin(request)
            state = await output.state()
            if state is None:
                raise HTTPException(502, output.status["error"] or f"{output.name} not reachable")
            return state

        async def status(request: Request):
            require_admin(request)
            return await output.refresh()

        async def overlay(request: Request):
            require_admin(request)
            on = bool((await request.json()).get("on"))
            if not await output.overlay(on):
                raise HTTPException(502, output.status["error"] or f"Choose what to show in {output.name} first")
            return output.status

        async def test(request: Request):
            require_admin(request)
            text = (await request.json()).get("text") or "Live Translate ✓"
            if not await output.show(text):
                raise HTTPException(502, output.status["error"] or f"Choose what to show in {output.name} first")
            return output.status

        app.add_api_route(f"/api/admin/{name}/inputs", inputs)
        app.add_api_route(f"/api/admin/{name}/status", status)
        app.add_api_route(f"/api/admin/{name}/overlay", overlay, methods=["POST"])
        app.add_api_route(f"/api/admin/{name}/test", test, methods=["POST"])

    streaming_routes("vmix", services.vmix)
    streaming_routes("obs", services.obs)

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
                # Back into English, so an operator who can't read the language can check the meaning.
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
        require_local_admin(request)
        try:
            await services.updater.install()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return services.updater.status()

    @app.post("/api/admin/tunnel/key")
    async def tunnel_key(request: Request):
        """Create this PC's key pair for the tunnel (once); returns the tunnel status with the public key."""
        require_local_admin(request)
        try:
            await services.tunnel.create_key()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return services.tunnel.status()

    @app.post("/api/admin/tunnel/forget")
    async def tunnel_forget(request: Request):
        """Forget the server's identity (after reinstalling it) and connect again."""
        require_local_admin(request)
        services.tunnel.forget_server()
        remote = services.settings.remote
        await services.tunnel.stop()
        await services.tunnel.apply(remote)
        return services.tunnel.status()

    @app.post("/api/admin/upload")
    async def upload(request: Request):
        """Store an audio file for rehearsals. Body is the raw file."""
        require_local_admin(request)
        name = re.sub(r"[^\w.\- ]", "_", request.query_params.get("name", "audio.wav"))[:100]
        folder = services.data / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with open(path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        return {"path": str(path)}

    # -- Settings → Web Server (only on this PC) --------------------------------

    async def web_server_state() -> dict:
        s = services.settings
        ws = s.web_server
        return {
            "lan_admin": ws.lan_admin,
            "tunnel_on": s.remote.enabled,
            "password_set": bool(ws.password_hash),
            "password_short": ws.password_short,
            "min_password": MIN_PASSWORD,
            "running_port": services.port,
            "saved_port": s.port,
            "firewall": await asyncio.to_thread(firewall.status, s.port),
            "firewall_change": services.firewall_change,
            "can_restart": services.restart is not None,
            "admin_urls": await asyncio.to_thread(services.admin_urls) if remote_admin_on() else [],
            "sessions": [
                {k: row[k] for k in ("id", "created", "last_seen", "ip", "ua")} for row in services.sessions.list()
            ],
        }

    def save_web_server(**changes) -> None:
        s = services.settings
        services.store.save(s.model_copy(update={"web_server": s.web_server.model_copy(update=changes)}))

    @app.get("/api/admin/webserver")
    async def get_web_server(request: Request):
        require_local_admin(request)
        return await web_server_state()

    @app.post("/api/admin/webserver/password")
    async def set_password(request: Request):
        require_local_admin(request)
        password = str((await request.json()).get("password") or "")
        if len(password) < MIN_PASSWORD:
            raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters.")
        if len(password) > 200:
            raise HTTPException(400, "That password is too long.")
        save_web_server(password_hash=await asyncio.to_thread(hash_password, password), password_short=False)
        end_sessions()  # whoever knew the old one is out
        log.info("Admin password changed; every device signs in again")
        return await web_server_state()

    @app.post("/api/admin/webserver/lan")
    async def set_lan_admin(request: Request):
        require_local_admin(request)
        on = bool((await request.json()).get("on"))
        if on and not services.settings.web_server.password_hash:
            raise HTTPException(400, "Choose a password first.")
        save_web_server(lan_admin=on)
        if not on:
            end_sessions()
        log.info("Admin page from the network %s", "allowed" if on else "switched off")
        return await web_server_state()

    @app.post("/api/admin/webserver/port")
    async def set_port(request: Request):
        require_local_admin(request)
        try:
            port = int((await request.json()).get("port"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Not a port number.") from exc
        s = services.settings
        if not 1024 <= port <= 65535:
            raise HTTPException(400, "Choose a port from 1024 to 65535.")
        if port in (s.remote.local_port, s.libretranslate_port):
            raise HTTPException(400, f"Port {port} is already used by Live Translate itself.")
        if port != services.port and not await asyncio.to_thread(_port_free, port):
            raise HTTPException(409, f"Another program is using port {port}.")
        if port != s.port:
            services.store.save(s.model_copy(update={"port": port}))
            services.firewall_change = ""
            log.info("Port changed to %d; used after a restart", port)
        return await web_server_state()

    @app.post("/api/admin/webserver/firewall")
    async def fix_firewall(request: Request):
        require_local_admin(request)
        port = services.settings.port
        services.firewall_change = await asyncio.to_thread(firewall.update_rule, port)
        if services.firewall_change == "ok" and port == services.port:
            services.firewall_problem = False
        return await web_server_state()

    @app.post("/api/admin/webserver/restart")
    async def restart_app(request: Request):
        require_local_admin(request)
        if services.pipeline.state != "stopped":
            raise HTTPException(409, "Stop translating first.")
        if services.restart is None:
            raise HTTPException(503, "Start Live Translate again yourself to use the new port.")
        log.info("Restarting for port %d", services.settings.port)
        asyncio.get_running_loop().call_later(0.5, services.restart)
        return {"ok": True, "port": services.settings.port}

    @app.delete("/api/admin/webserver/sessions/{session_id}")
    async def sign_out_device(session_id: str, request: Request):
        require_local_admin(request)
        end_sessions(session_id)
        return await web_server_state()

    @app.post("/api/admin/webserver/sessions/revoke-all")
    async def sign_out_all(request: Request):
        require_local_admin(request)
        end_sessions()
        return await web_server_state()

    @app.websocket("/ws/admin")
    async def admin_ws(ws: WebSocket):
        try:
            access = require_admin(ws)
        except HTTPException as exc:
            await ws.close(code=4401 if exc.status_code == 401 else 4403)
            return
        await ws.accept()
        conn = Connection(ws)
        if access == "remote":
            conn.session_id = ws.state.session_id
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


async def _check_firewall(services: Services) -> None:
    """A rule for another port (the port was changed, or an old installer reset the rule) keeps
    phones out; the operator page says so. No rule at all is not certain: Windows may have asked
    to let Python through when the app first started."""
    status = await asyncio.to_thread(firewall.status, services.port)
    if status == "mismatch":
        log.warning("The Windows Firewall rule doesn't open port %d; phones may not connect", services.port)
        services.firewall_problem = True
    elif status == "missing":
        log.info("There is no Windows Firewall rule for Live Translate")


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
                "obs": services.obs.status,
                "update": services.updater.status(),
                "tunnel": services.tunnel.status(),
            }
        )
