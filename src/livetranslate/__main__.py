"""Start Live Translate: python -m livetranslate [--port 8000] [--no-browser]"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from .config import SettingsStore, data_dir

    parser = argparse.ArgumentParser(prog="livetranslate", description="Live translation of church services")
    parser.add_argument("--port", type=int, help="web server port (default from settings: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the admin page")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    data = data_dir()
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(data / "livetranslate.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"),
    ]
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

    from .stt.cuda import setup_cuda_dll_paths

    setup_cuda_dll_paths()

    import uvicorn

    from .server.app import create_app

    app = create_app(store, data)
    admin_url = f"http://127.0.0.1:{port}/admin"
    print(f"\n  Live Translate is starting. Operator page: {admin_url}\n", flush=True)
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(admin_url)), daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", ws_ping_interval=20)


if __name__ == "__main__":
    main()
