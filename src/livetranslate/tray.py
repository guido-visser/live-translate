"""System tray icon: shows that Live Translate is running, opens the pages, exits cleanly."""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable

from PIL import Image, ImageDraw

from .languages import ui_language

log = logging.getLogger(__name__)

COLORS = {
    "stopped": (120, 126, 138),
    "loading": (42, 109, 244),
    "running": (31, 157, 85),
    "paused": (217, 138, 0),
    "testimony": (217, 138, 0),
}

TEXTS = {
    "nl": {
        "open": "Bedieningspagina openen",
        "qr": "QR-code tonen",
        "exit": "Afsluiten",
        "stopped": "Gestopt",
        "loading": "Model laden…",
        "running": "Bezig",
        "paused": "Gepauzeerd",
        "testimony": "Getuigenis",
        "listeners": "luisteraars",
    },
    "en": {
        "open": "Open operator page",
        "qr": "Show QR code",
        "exit": "Exit",
        "stopped": "Stopped",
        "loading": "Loading model…",
        "running": "Running",
        "paused": "Paused",
        "testimony": "Testimony",
        "listeners": "listeners",
    },
    "ru": {
        "open": "Открыть панель управления",
        "qr": "Показать QR-код",
        "exit": "Выход",
        "stopped": "Остановлено",
        "loading": "Загрузка модели…",
        "running": "Работает",
        "paused": "Пауза",
        "testimony": "Свидетельство",
        "listeners": "слушателей",
    },
    "uk": {
        "open": "Відкрити панель керування",
        "qr": "Показати QR-код",
        "exit": "Вийти",
        "stopped": "Зупинено",
        "loading": "Завантаження моделі…",
        "running": "Працює",
        "paused": "Пауза",
        "testimony": "Свідчення",
        "listeners": "слухачів",
    },
}


def make_icon(state: str, size: int = 64) -> Image.Image:
    """The app's icon as drawn for small sizes (packaging/icon/icon-small.svg); the colour shows the state."""
    u = size * 4 / 96  # on the drawing's grid; draw large and shrink for smooth edges
    color = COLORS.get(state, COLORS["stopped"])
    white = (255, 255, 255, 255)
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 96 * u - 1, 96 * u - 1), radius=20 * u, fill=color)
    d.rounded_rectangle((12 * u, 12 * u, 84 * u, 66 * u), radius=12 * u, fill=white)
    d.polygon([(28 * u, 60 * u), (28 * u, 84 * u), (44 * u, 66 * u)], fill=white)
    for x, y, width, height in ((24, 30, 6, 18), (36, 24, 6, 30), (48, 30, 24, 6), (48, 42, 18, 6)):
        d.rectangle((x * u, y * u, (x + width) * u - 1, (y + height) * u - 1), fill=color)
    return img.resize((size, size), Image.LANCZOS)


def tray_state(status: dict, main_language: str) -> str:
    if status["state"] != "running":
        return status["state"]
    if status["paused"]:
        return "paused"
    if status.get("speaker_language", main_language) != main_language:
        return "testimony"
    return "running"


def status_text(state: str, listeners: int, texts: dict) -> str:
    text = texts[state]
    if state in ("running", "paused", "testimony"):
        text += f" · {listeners} {texts['listeners']}"
    return text


class Tray:
    def __init__(
        self,
        get_status: Callable[[], tuple[str, int]],
        admin_url: str,
        qr_url: str,
        on_exit: Callable[[], None],
        language: str | None = None,
        open_page: Callable[[str], None] = webbrowser.open,
    ):
        self.get_status = get_status  # -> (tray state, number of listeners)
        self.admin_url = admin_url
        self.qr_url = qr_url
        self.on_exit = on_exit
        self.open_page = open_page
        self.texts = TEXTS[language or ui_language()]
        self._state = ("", -1)
        self._stop = threading.Event()
        self.icon = None

    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Live Translate – {self._status_line()}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self.texts["open"], self._open_admin, default=True),
            pystray.MenuItem(self.texts["qr"], self._open_qr),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self.texts["exit"], self._exit),
        )

    def _status_line(self) -> str:
        state, listeners = self._state
        return status_text(state or "stopped", max(listeners, 0), self.texts)

    def _open_admin(self, icon=None, item=None) -> None:
        threading.Thread(target=self.open_page, args=(self.admin_url,), daemon=True).start()

    def _open_qr(self, icon=None, item=None) -> None:
        webbrowser.open(self.qr_url)

    def _exit(self, icon=None, item=None) -> None:
        log.info("Exit chosen from the tray")
        try:
            self.on_exit()
        finally:
            self.stop()

    def stop(self) -> None:
        """Remove the icon; run() then returns."""
        self._stop.set()
        if self.icon is not None:
            self.icon.stop()

    def _refresh(self) -> None:
        """Keep the icon colour, tooltip and status line in step with the app."""
        while not self._stop.wait(1.0):
            try:
                current = self.get_status()
            except Exception:
                log.debug("Tray status failed", exc_info=True)
                continue
            if current == self._state or self.icon is None:
                continue
            self._state = current
            self.icon.icon = make_icon(current[0])
            self.icon.title = f"Live Translate – {self._status_line()}"
            self.icon.update_menu()

    def run(self) -> None:
        """Blocks until Exit is chosen. Must run on the main thread (required on macOS)."""
        import pystray

        self._state = self.get_status()
        self.icon = pystray.Icon(
            "LiveTranslate", make_icon(self._state[0]), f"Live Translate – {self._status_line()}", self._menu()
        )
        threading.Thread(target=self._refresh, name="tray", daemon=True).start()
        log.info("Tray icon ready")
        self.icon.run()
