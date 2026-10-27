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
    """A coloured circle with a white speech bubble; the colour shows the state."""
    scale = 4  # draw large and shrink for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=COLORS.get(state, COLORS["stopped"]))
    white = (255, 255, 255, 255)
    d.rounded_rectangle((s * 0.22, s * 0.26, s * 0.78, s * 0.62), radius=s * 0.08, fill=white)
    d.polygon([(s * 0.34, s * 0.60), (s * 0.30, s * 0.76), (s * 0.48, s * 0.60)], fill=white)
    bar = COLORS.get(state, COLORS["stopped"])
    for i, width in enumerate((0.40, 0.28)):
        y = s * (0.36 + i * 0.12)
        d.rounded_rectangle((s * 0.30, y, s * (0.30 + width), y + s * 0.05), radius=s * 0.025, fill=bar)
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
    ):
        self.get_status = get_status  # -> (tray state, number of listeners)
        self.admin_url = admin_url
        self.qr_url = qr_url
        self.on_exit = on_exit
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
        webbrowser.open(self.admin_url)

    def _open_qr(self, icon=None, item=None) -> None:
        webbrowser.open(self.qr_url)

    def _exit(self, icon=None, item=None) -> None:
        log.info("Exit chosen from the tray")
        self._stop.set()
        try:
            self.on_exit()
        finally:
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
