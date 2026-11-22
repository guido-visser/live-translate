"""Makes the icon files from the two drawings here, with Edge. Run after changing them:
uv run python packaging/icon/make_icons.py

The listener page's home-screen icons (web/client/) fill the whole square: phones cut their own
shape out of them. The maskable one keeps the drawing inside the middle 80%, which Android may crop to.

Small sizes get icon-small.svg: on a grid of whole pixels at 16 px, because the details of icon.svg
turn to mush there. tray.py draws the same small design in the colour of the app's state.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HERE = Path(__file__).parent
ROOT = HERE.parents[1]
SMALL = (16, 20, 24)
ICO_SIZES = (*SMALL, 32, 40, 48, 64, 128, 256)
WEB_SIZE = 512
WINDOW = 1024  # Edge has a minimum window size, so each picture is cut out of a large one


def full_bleed(drawing: str, scale: float = 1.0) -> str:
    """icon.svg without its rounded corners; the drawing on it shrunk around the middle by scale."""
    drawing = drawing.replace('<rect width="96" height="96" rx="22"', '<rect width="96" height="96"', 1)
    if scale != 1.0:
        head, rest = drawing.split("<path", 1)
        body, tail = rest.rsplit("</svg>", 1)
        drawing = f'{head}<g transform="translate(48 48) scale({scale}) translate(-48 -48)"><path{body}</g></svg>{tail}'
    return drawing


def render(size: int, folder: Path, drawing: str | None = None) -> Image.Image:
    if drawing is None:
        drawing = (HERE / ("icon-small.svg" if size in SMALL else "icon.svg")).read_text("utf-8")
    drawing = drawing.replace("<svg ", f'<svg width="{size}" height="{size}" style="position:absolute;left:0;top:0" ', 1)
    page, shot = folder / "page.html", folder / "shot.png"
    page.write_text(f"<!doctype html><body style='margin:0;background:transparent'>{drawing}", "utf-8")
    subprocess.run(
        [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",
            f"--user-data-dir={folder / 'profile'}",
            f"--window-size={WINDOW},{WINDOW}",
            f"--screenshot={shot}",
            page.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    with Image.open(shot) as picture:
        return picture.convert("RGBA").crop((0, 0, size, size))


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        render(WEB_SIZE, folder).save(ROOT / "src" / "livetranslate" / "web" / "icon.png", optimize=True)
        client = ROOT / "src" / "livetranslate" / "web" / "client"
        big = (HERE / "icon.svg").read_text("utf-8")
        for name, size, drawing in (
            ("icon-192.png", 192, full_bleed(big)),
            ("icon-512.png", 512, full_bleed(big)),
            ("icon-maskable-512.png", 512, full_bleed(big, 0.8)),
            ("apple-touch-icon.png", 180, full_bleed(big)),
        ):
            render(size, folder, drawing).convert("RGB").save(client / name, optimize=True)
        images = [render(size, folder) for size in ICO_SIZES]
    largest = images[-1]
    for path in (ROOT / "src" / "livetranslate" / "web" / "icon.ico", ROOT / "packaging" / "windows" / "LiveTranslate.ico"):
        largest.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=images[:-1])


if __name__ == "__main__":
    main()
