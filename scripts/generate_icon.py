"""Genera assets/icon.ico a partir del mismo dibujo que usa la bandeja
(wispi/tray.py:_make_icon), en gris IDLE: circulo solido con un punto claro
en el centro, forma de "microfono encendido". Un solo .ico multi-resolucion
para que Windows elija el tamano correcto en escritorio, taskbar y Alt+Tab.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon.ico"

RGB_IDLE = (110, 118, 129)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def make_icon(rgb: tuple[int, int, int], size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 8
    d.ellipse([m, m, size - m, size - m], fill=rgb + (255,))
    c, r = size // 2, size // 7
    d.ellipse([c - r, c - r, c + r, c + r], fill=(250, 250, 250, 235))
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = make_icon(RGB_IDLE, 256)
    base.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Generado: {OUT}")


if __name__ == "__main__":
    main()
