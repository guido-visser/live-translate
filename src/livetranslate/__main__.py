"""Start Live Translate: python -m livetranslate [--port 8000] [--no-browser] [--no-tray]"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

log = logging.getLogger("livetranslate")

# A phone's audio stream never ends by itself; without a limit it would keep the app from
# closing (and an update's installer waiting) until that phone leaves.
SHUTDOWN_WAIT_S = 3
# How often the app may start itself again in a row before it leaves that to a person.
MAX_RESTARTS = 3
# How long a restarted app waits for the old one to close.
AFTER_WAIT_S = 120

# Kept for the life of the process.
_instance_mutex = None
_crash_log = None


def already_running(port: int) -> bool:
    """Is Live Translate already serving on this port? (Then we just open its page.)"""
    import httpx

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


def single_instance(port: int) -> bool:
    """Claim this port for one app process (Windows); False when another one already has it.

    Two starts at nearly the same time both pass already_running() and then race for the
    port; the loser used to die with "address already in use"."""
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\LiveTranslate-{port}")
    if handle and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    global _instance_mutex
    _instance_mutex = handle  # held until the process ends
    return True


def instance_starting(port: int) -> bool:
    """Does another process hold this port's claim (see single_instance)?"""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, f"Local\\LiveTranslate-{port}")  # SYNCHRONIZE
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def wait_for_exit(pid: int, timeout_s: float) -> None:
    """A restart: the old process has to let go of the port first."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout_s * 1000))
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def restart_command(argv: list[str], pid: int, resume: bool) -> list[str]:
    """This app again, started as it was, after this process (pid) has ended."""
    args, restarts, i = [], 0, 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--after", "--restarts") and i + 1 < len(argv):
            if arg == "--restarts":
                restarts = int(argv[i + 1])
            i += 2
            continue
        if arg != "--resume":
            args.append(arg)
        i += 1
    command = [sys.executable, "-m", "livetranslate", *args, "--after", str(pid), "--restarts", str(restarts + 1)]
    return [*command, "--resume"] if resume else command


def lower_priority() -> None:
    """When the PC is busy the livestream goes first (LibreTranslate inherits this)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS


def enable_crash_log(path) -> None:
    """Native crashes (e.g. in a DLL) kill the process without a Python traceback; this
    writes the Python stack of every thread to crash.log when that happens."""
    try:
        crash_log = open(path, "a", encoding="utf-8")
        crash_log.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} start, pid {os.getpid()}\n")
        crash_log.flush()
        faulthandler.enable(crash_log, all_threads=True)
        global _crash_log
        _crash_log = crash_log  # must stay open
    except OSError:
        log.warning("Could not open %s", path, exc_info=True)


def make_server(app, port: int):
    import uvicorn

    # log_config=None: uvicorn's own console logging breaks without a console (pythonw).
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_config=None,
            log_level="warning",
            ws_ping_interval=20,
            timeout_graceful_shutdown=SHUTDOWN_WAIT_S,
        )
    )


def main() -> None:
    console = ensure_streams()

    from . import autostart
    from .config import SettingsStore, data_dir, keep_downloads_in_data_dir
    from .window import open_page

    keep_downloads_in_data_dir()  # before anything reads these, and LibreTranslate inherits them

    parser = argparse.ArgumentParser(prog="livetranslate", description="Live translation of church services")
    parser.add_argument("--port", type=int, help="web server port (default from settings: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the admin page, tray icon only")
    parser.add_argument("--autostart", action="store_true", help="started with Windows: show what the settings say")
    parser.add_argument("--no-tray", action="store_true", help="no system tray icon (e.g. on a server)")
    parser.add_argument("--debug", action="store_true")
    # When the app starts itself again (see health.py):
    parser.add_argument("--after", type=int, help=argparse.SUPPRESS)  # wait for this process to end
    parser.add_argument("--restarts", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)  # it was translating
    args = parser.parse_args()

    from . import splash

    # Something on screen right away when someone started it, before the slow part.
    starting = None
    if not args.no_browser and not args.autostart:
        starting = splash.show(os.getpid())

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

    enable_crash_log(data / "crash.log")
    if args.after:
        log.info("Started again by the app (%d); waiting for process %d to close", args.restarts, args.after)
        wait_for_exit(args.after, AFTER_WAIT_S)

    store = SettingsStore()
    if args.port:
        store.settings.port = args.port
        store.save(store.settings)
    port = store.settings.port
    admin_url = f"http://127.0.0.1:{port}/admin"

    start_window = store.settings.start_window if args.autostart else "normal"
    show_page = not args.no_browser and start_window != "tray"
    if starting is None and show_page:
        starting = splash.show(os.getpid())

    def open_admin(url: str = admin_url, minimized: bool = False) -> None:
        open_page(url, store.settings.app_window, minimized=minimized)

    if already_running(port):
        log.info("Live Translate is already running; opening its page")
        splash.close(starting)
        if show_page:
            open_admin()
        return
    if not single_instance(port):
        # However long it takes; the splash of the one that's starting (or this one's) stays up.
        log.info("Live Translate is already starting; opening its page when it is ready")
        while instance_starting(port) and not already_running(port):
            time.sleep(1)
        splash.close(starting)
        if show_page and already_running(port):
            open_admin()
        return

    lower_priority()
    if store.settings.start_with_windows:
        autostart.apply(True)  # the install folder may have changed

    from .stt.cuda import setup_cuda_dll_paths

    setup_cuda_dll_paths()

    from .server.app import create_app

    app = create_app(store, data, resume=args.resume)
    server = make_server(app, port)
    log.info("Live Translate is starting. Operator page: %s", admin_url)

    # Phones on the network use the main port; the internet tunnel ends on a second port,
    # only on this PC, where the operator page is refused (see TunnelGuard).
    try:
        sockets = [socket.create_server(("0.0.0.0", port))]
    except OSError as exc:
        log.error("Port %d is in use (%s); is another program using it?", port, exc)
        return
    tunnel_port = store.settings.remote.local_port
    try:
        sockets.append(socket.create_server(("127.0.0.1", tunnel_port)))
    except OSError as exc:
        log.warning("Tunnel port %d is in use (%s); listeners over the internet won't work", tunnel_port, exc)
        app.state.services.tunnel.local_port = 0

    tray = None if args.no_tray else _make_tray(app, server, port, open_admin)

    def request_exit() -> None:
        """Close the whole app (used when an update's installer takes over)."""
        server.should_exit = True
        if tray is not None:
            tray.stop()

    def on_ready() -> None:
        splash.close(starting)
        if show_page:
            open_admin(minimized=start_window == "minimized")

    def on_broken(problem: str) -> None:
        splash.close(starting)
        if args.restarts >= MAX_RESTARTS:
            log.error("Started again %d times already; not again. Close Live Translate and start it.", args.restarts)
            return
        import subprocess

        resume = app.state.services.pipeline.state != "stopped"
        log.error("Starting Live Translate again to fix this%s", " (translation goes on)" if resume else "")
        try:
            subprocess.Popen(restart_command(sys.argv[1:], os.getpid(), resume))
        except OSError:
            log.exception("Could not start Live Translate again")
            return
        request_exit()

    from .health import Watchdog, check

    Watchdog(
        is_started=lambda: server.started,
        is_stopping=lambda: server.should_exit,
        on_ready=on_ready,
        on_broken=on_broken,
        check=lambda: check(port),
        report=lambda problem: setattr(app.state, "pages_problem", problem),
    ).start()

    app.state.services.updater.request_exit = request_exit
    if tray is None:
        server.run(sockets=sockets)
        return

    server_thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, name="server")
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


def _make_tray(app, server, port: int, open_admin):
    try:
        from .tray import Tray, tray_state
    except Exception:
        log.warning("No system tray available", exc_info=True)
        return None
    services = app.state.services

    def get_status() -> tuple[str, int]:
        if getattr(app.state, "pages_problem", None):
            return "problem", 0  # red, see health.py
        state = tray_state(services.pipeline.status(), services.settings.source_language)
        listeners = sum(services.hub.counts().values())
        audio_only = sum(services.speech.listener_counts().values())
        return state, listeners if listeners else audio_only

    def on_exit() -> None:
        server.should_exit = True

    admin_url, qr_url = f"http://127.0.0.1:{port}/admin", f"http://127.0.0.1:{port}/qr"
    return Tray(get_status, admin_url, qr_url, on_exit, open_page=open_admin)


if __name__ == "__main__":
    main()
