"""Start Live Translate: python -m livetranslate [--port 8000] [--no-browser] [--no-tray]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

import httpx

log = logging.getLogger("livetranslate")


def already_running(port: int) -> bool:
    """Is Live Translate already serving on this port? (Then we just open its page.)"""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/public", timeout=1.5)
        return r.status_code == 200 and "url" in r.json()
    except (httpx.HTTPError, ValueError):
        return False


def ensure_streams() -> bool:
    """Make stdout/stderr writable; returns whether there is a real console.

    Under pythonw (no console window) both are None, and libraries that print progress
    (e.g. the tqdm bars of Hugging Face model downloads) then crash on first start."""
    console = sys.stdout is not None
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return console


def main() -> None:
    console = ensure_streams()

    from .config import SettingsStore, data_dir

    parser = argparse.ArgumentParser(prog="livetranslate", description="Live translation of church services")
    parser.add_argument("--port", type=int, help="web server port (default from settings: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the admin page")
    parser.add_argument("--no-tray", action="store_true", help="no system tray icon (e.g. on a server)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    data = data_dir()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(data / "livetranslate.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"),
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )

    store = SettingsStore()
    if args.port:
        store.settings.port = args.port
        store.save(store.settings)
    port = store.settings.port
    admin_url = f"http://127.0.0.1:{port}/admin"

    if already_running(port):
        log.info("Live Translate is already running; opening its page")
        webbrowser.open(admin_url)
        return

    from .stt.cuda import setup_cuda_dll_paths

    setup_cuda_dll_paths()

    import uvicorn

    from .server.app import create_app

    app = create_app(store, data)
    # log_config=None: uvicorn's own console logging breaks without a console (pythonw).
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, log_level="warning", ws_ping_interval=20)
    )
    log.info("Live Translate is starting. Operator page: %s", admin_url)

    def open_browser_when_ready() -> None:
        for _ in range(100):
            if server.started:
                webbrowser.open(admin_url)
                return
            time.sleep(0.2)

    if not args.no_browser:
        threading.Thread(target=open_browser_when_ready, daemon=True).start()

    tray = None if args.no_tray else _make_tray(app, server, port)

    def request_exit() -> None:
        """Close the whole app (used when an update's installer takes over)."""
        server.should_exit = True
        if tray is not None:
            tray.stop()

    app.state.services.updater.request_exit = request_exit
    if tray is None:
        server.run()
        return

    server_thread = threading.Thread(target=server.run, name="server")
    server_thread.start()
    try:
        tray.run()
    except Exception:
        log.exception("Tray icon failed; the app keeps running without it")
        server_thread.join()
        return
    # Exit was chosen: let the server shut down (this also stops LibreTranslate).
    server.should_exit = True
    server_thread.join(timeout=30)


def _make_tray(app, server, port: int):
    try:
        from .tray import Tray, tray_state
    except Exception:
        log.warning("No system tray available", exc_info=True)
        return None
    services = app.state.services

    def get_status() -> tuple[str, int]:
        state = tray_state(services.pipeline.status(), services.settings.source_language)
        listeners = sum(services.hub.counts().values())
        audio_only = sum(services.speech.listener_counts().values())
        return state, listeners if listeners else audio_only

    def on_exit() -> None:
        server.should_exit = True

    return Tray(get_status, f"http://127.0.0.1:{port}/admin", f"http://127.0.0.1:{port}/qr", on_exit)


if __name__ == "__main__":
    main()
