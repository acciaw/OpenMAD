"""Build assets/icon.ico from assets/icon.png.

Windows wants a square, multi resolution icon file. Source art is rarely
exactly square, so this trims it to its visible pixels and then pads it onto a
square canvas. It never stretches, because a stretched tray icon looks subtly
wrong next to every other icon in the tray.

Run it after changing icon.png:

    python packaging/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "assets" / "icon.png"
DEST = REPO / "assets" / "icon.ico"

# Windows picks the closest entry for each context: 16 in the tray and title
# bar, 32 on the desktop, 48 in Explorer, 256 for large tiles.
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Breathing room around the art, as a fraction of the square. Icons that bleed
# to the very edge read as larger than their neighbours.
PAD = 0.06


def square(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    side = max(img.size)
    canvas = int(side * (1 + PAD * 2))
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(img, ((canvas - img.width) // 2, (canvas - img.height) // 2), img)
    return out


def main() -> int:
    if not SRC.exists():
        print(f"missing {SRC}", file=sys.stderr)
        return 1

    src = Image.open(SRC)
    print(f"source  {src.size[0]}x{src.size[1]} {src.mode}")

    base = square(src)
    print(f"squared {base.size[0]}x{base.size[1]}, trimmed and padded, not stretched")

    # Pillow's icon writer ignores append_images and would silently produce a
    # single entry file. Passing sizes to one image is the supported route.
    base = base.resize((max(SIZES), max(SIZES)), Image.LANCZOS)
    base.save(DEST, format="ICO", sizes=[(s, s) for s in SIZES])

    written = sorted(Image.open(DEST).ico.sizes())
    print(f"wrote   {DEST.relative_to(REPO)}, {DEST.stat().st_size / 1024:.1f} KB")
    print(f"entries {[s[0] for s in written]}")
    if len(written) != len(SIZES):
        print(f"expected {len(SIZES)} entries, got {len(written)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
