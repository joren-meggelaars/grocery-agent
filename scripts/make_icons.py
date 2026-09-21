"""Regenerates the app icons (a barcode on the accent colour). Run: uv run python scripts/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src/grocery/web/static/icons"
BARS = [3, 1, 2, 1, 4, 1, 1, 2, 3, 1, 2, 2, 1, 3, 1, 1, 2, 1, 4]  # alternating bar/space widths


def draw(size: int) -> Image.Image:
    # black in one corner, blue in the other, like the app's hero card
    img = Image.new("RGB", (size, size))
    px = img.load()
    start, end = (7, 11, 20), (37, 99, 235)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    d = ImageDraw.Draw(img)
    unit = size * 0.56 / sum(BARS)
    x, top, bottom = size * 0.22, size * 0.24, size * 0.76
    for i, width in enumerate(BARS):
        w = width * unit
        if i % 2 == 0:
            d.rectangle([x, top, x + w, bottom], fill="white")
        x += w
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        draw(size).save(OUT / name, optimize=True)
        print("wrote", name)
