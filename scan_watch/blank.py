"""Blank page detection."""

import io

from PIL import Image


def content_score(jpeg, dpi):
    """Ink coverage (in %) of the densest ~8 mm tile of the page.

    Text clusters into dense tiles, while dust and JPEG noise spread thinly,
    so the densest tile separates "nearly empty" from "truly blank" far better
    than the ink coverage of the whole page.
    """
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    w, h = img.size
    # Ignore borders where feeder shadows, edges and punch holes show up.
    mx, my = int(w * 0.05), int(h * 0.05)
    img = img.crop((mx, my, w - mx, h - my))
    hist = img.histogram()
    # Paper color = most frequent light tone; anything clearly darker is ink.
    # Faint bleed-through from the other side stays above the limit.
    background = max(range(128, 256), key=lambda v: hist[v])
    limit = max(0, background - 60)
    ink = img.point(lambda v: 255 if v < limit else 0)
    tile = max(1, round(dpi / 3))
    grid = (max(1, ink.width // tile), max(1, ink.height // tile))
    tiles = ink.resize(grid, Image.Resampling.BOX)
    return 100.0 * tiles.getextrema()[1] / 255
