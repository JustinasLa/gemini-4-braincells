"""Draw the Shill or Chill icon set into public/ (one source for every size).

    cd web/trackdown && uvx --python 3.12 --with pillow --with cairosvg python scripts/icons.py

The mark: Venus's scallop shell from The Birth of Venus, the origin a trace leads back to.
Its ribs split into the two verdicts, taken from the same painting: Flora's rose (shill) on
the left, the sea (chill) on the right. One shape and two colours on near-black, so it still
reads in a 16 px tab on light and dark browser chrome.
"""

import io
import math
from pathlib import Path

import cairosvg
from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "public"
BG = "#0b0b0d"
# Warm core -> brand colour -> deep edge, per verdict (--shill #e2533c, --chill #5fc9a8).
ROSE = ("#ffb39c", "#ec6a4f", "#a4321d")
SEA = ("#bff5e3", "#6fd4b4", "#2a7f69")
HINGE = "#c4823f"  # the coin's bronze


def shell() -> str:
    cx, hy, radius, span, n, bump = 16, 27.4, 18.6, 48, 9, 1.3
    angles = [math.radians(-span + 2 * span * i / (n - 1)) for i in range(n)]

    def pt(a: float, r: float) -> str:
        return f"{cx + r * math.sin(a):.2f} {hy - r * math.cos(a):.2f}"

    ribs = [
        f"M{cx} {hy}L{pt(a, radius)}Q{pt((a + b) / 2, radius + 2 * bump)} {pt(b, radius)}Z"
        for a, b in zip(angles, angles[1:], strict=False)
    ]
    half = len(ribs) // 2
    out = f'<path d="M10.6 30L13.6 26.4H18.4L21.4 30Z" fill="{HINGE}"/>'
    groove = 'fill="none" stroke="#000" stroke-opacity=".35" stroke-width=".4" '
    groove += 'stroke-linejoin="round"'
    for i, d in enumerate(ribs):
        side = "rose" if i < half else "sea"
        out += f'<path d="{d}" fill="url(#{side})"/><path d="{d}" fill="url(#shade)"/>'
    out += "".join(f'<path d="{d}" {groove}/>' for d in ribs)
    return out


def gradient(name: str, stops: tuple[str, str, str]) -> str:
    a, b, c = stops
    return (
        f'<radialGradient id="{name}" cx="16" cy="28" r="20" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{a}"/><stop offset=".5" stop-color="{b}"/>'
        f'<stop offset="1" stop-color="{c}"/>'
        "</radialGradient>"
    )


def svg(rx: float = 7, scale: float = 1) -> str:
    """rx: tile corner radius (0 = full bleed). scale < 1 shrinks the shell into a safe zone."""
    defs = (
        "<defs>"
        + gradient("rose", ROSE)
        + gradient("sea", SEA)
        + '<linearGradient id="shade" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#000" stop-opacity=".35"/>'
        '<stop offset=".35" stop-color="#000" stop-opacity="0"/>'
        '<stop offset="1" stop-color="#000" stop-opacity=".15"/></linearGradient>'
        "</defs>"
    )
    mark = shell()
    if scale != 1:
        off = 16 * (1 - scale)
        mark = f'<g transform="translate({off:g} {off:g}) scale({scale:g})">{mark}</g>'
    tile = f'<rect width="32" height="32" rx="{rx}" fill="{BG}"/>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">{defs}{tile}{mark}</svg>\n'


def png(source: str, size: int) -> Image.Image:
    data = cairosvg.svg2png(bytestring=source.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(data)).convert("RGBA")


tab = svg()
(OUT / "favicon.svg").write_text(tab)
png(tab, 48).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
# iOS rounds the corners itself and shows transparency as black: full-bleed tile, a little inset.
png(svg(rx=0, scale=0.86), 180).save(OUT / "apple-touch-icon.png", optimize=True)
for size in (192, 512):
    png(tab, size).save(OUT / f"icon-{size}.png", optimize=True)
# Android masks may crop to a circle of 80% diameter: keep the shell inside it.
png(svg(rx=0, scale=0.7), 512).save(OUT / "icon-maskable-512.png", optimize=True)
print(f"Icons written to {OUT}")
