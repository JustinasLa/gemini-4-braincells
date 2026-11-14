# Cover the nudity in the Birth of Venus backdrop: each pinned keypoint in src/assets/venus-full-cv.json
# ("pin": true) gets a soft, feathered smudge of the skin under it, so the dim case-view backdrop shows
# smooth painted skin with no edges. The home banner draws the pins as live detection boxes on top
# (src/ui/cvbanner.ts), warped like every other box.
# Run once on the uncovered painting:
#   uv run --with pillow python blender/cover_venus.py <uncovered.webp> src/assets/venus-full.webp
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

CV = Path(__file__).resolve().parent.parent / "src/assets/venus-full-cv.json"
REACH = 1.35  # the smudge reaches past the pin's box, so the box never shows its edge


def cover(src: str, dst: str) -> None:
    im = Image.open(src).convert("RGB")
    cv = json.loads(CV.read_text())
    for p in (q for q in cv["points"] if q.get("pin")):
        side = p["s"] * im.width
        cx, cy = p["x"] * im.width, p["y"] * im.height
        r = round(side * REACH / 2)
        box = (round(cx) - r, round(cy) - r, round(cx) + r, round(cy) + r)
        patch = im.crop(box)
        smudge = patch.filter(ImageFilter.GaussianBlur(side / 4)).filter(ImageFilter.GaussianBlur(side / 4))
        mask = Image.new("L", patch.size, 0)
        ImageDraw.Draw(mask).ellipse((r * 0.12, r * 0.12, r * 1.88, r * 1.88), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(r * 0.22))
        im.paste(Image.composite(smudge, patch, mask), box[:2])
    im.save(dst, "WEBP", quality=88, method=6)


if __name__ == "__main__":
    cover(sys.argv[1], sys.argv[2])
