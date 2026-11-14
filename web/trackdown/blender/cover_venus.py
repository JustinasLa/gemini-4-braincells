# Cover the nudity in the Birth of Venus backdrop with detection boxes in the Junction banner style:
# each box shows a magnified, swirled fragment of another part of the painting (drapery, cloak) in a
# green frame with a match score, so it reads as one more machine-vision detection, not a censor bar.
# Run once on the uncovered painting (re-running on a covered file copies frames into the fragments):
#   uv run --with pillow --with numpy python blender/cover_venus.py <uncovered.webp> src/assets/venus-full.webp
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# box: (left, top, width, height) to cover; src: (centre x, centre y) of the fragment shown inside;
# all in pixels of the 1920x1206 painting
BOXES = [
    {"box": (940, 326, 100, 104), "src": (505, 345), "score": "0.5714", "turn": 0.9},  # Venus, under her hand
    {"box": (1036, 334, 82, 96), "src": (1330, 470), "score": "0.7143", "turn": -0.8},  # Venus
    {"box": (542, 280, 96, 98), "src": (770, 255), "score": "0.2857", "turn": 0.7},  # Aura, in Zephyr's arms
]
ZOOM = 3.2
GREEN = (63, 224, 122)  # --cv, the UI's detection green


def fragment(im: Image.Image, cx: int, cy: int, w: int, h: int, turn: float) -> Image.Image:
    """A ZOOMx magnified patch around (cx, cy), swirled and rippled like liquid."""
    sw, sh = max(8, round(w / ZOOM)), max(8, round(h / ZOOM))
    src = np.asarray(im.crop((cx - sw // 2, cy - sh // 2, cx - sw // 2 + sw, cy - sh // 2 + sh)).resize((w * 2, h * 2), Image.LANCZOS))
    W, H = w * 2, h * 2
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    dx, dy = x - W / 2, y - H / 2
    r = np.hypot(dx, dy)
    a = np.arctan2(dy, dx) + turn * np.exp(-r / (0.45 * max(W, H)))  # swirl, strongest at the centre
    sx = W / 2 + r * np.cos(a) + 0.02 * W * np.sin(y / H * 2 * np.pi * 1.5)  # plus a slow ripple
    sy = H / 2 + r * np.sin(a) + 0.02 * H * np.sin(x / W * 2 * np.pi * 1.2)
    out = src[np.clip(sy, 0, H - 1).astype(int), np.clip(sx, 0, W - 1).astype(int)]
    return Image.fromarray(out).resize((w, h), Image.LANCZOS)


def font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def cover(src: str, dst: str) -> None:
    im = Image.open(src).convert("RGB")
    orig = im.copy()
    for b in BOXES:
        l, t, w, h = b["box"]
        im.paste(fragment(orig, *b["src"], w, h, b["turn"]), (l, t))
    draw = ImageDraw.Draw(im)
    label, mark = font(17), font(11)
    for b in BOXES:  # frames and labels after every fragment, so overlapping boxes keep both frames
        l, t, w, h = b["box"]
        draw.rectangle((l, t, l + w - 1, t + h - 1), outline=GREEN, width=3)
        cx, cy = l + w / 2, t + h / 2
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1), (2, 2)):  # dark edge, like the canvas text shadow
            draw.text((cx + dx, cy + dy), b["score"], font=label, fill=(0, 0, 0), anchor="mm")
        draw.text((cx, cy), b["score"], font=label, fill=(255, 255, 255), anchor="mm")
        draw.text((cx, cy + 17), "×", font=mark, fill=(235, 235, 235), anchor="mm")
    im.save(dst, "WEBP", quality=88, method=6)


if __name__ == "__main__":
    cover(sys.argv[1], sys.argv[2])
