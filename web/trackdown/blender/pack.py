"""Pack rendered coin frames into the web assets (needs Pillow + numpy).

python blender/coin.py frames 440 40 48      # Blender 4.2 bpy, ~5 s/frame on CPU
python blender/pack.py frames public/coin
"""

import sys

import numpy as np
from PIL import Image

src, out = sys.argv[1], sys.argv[2]
S, N, COLS, ROWS = 400, 48, 8, 6
sheet = Image.new("RGBA", (S * COLS, S * ROWS), (0, 0, 0, 0))
for i in range(N):
    im = Image.open(f"{src}/f{i:03d}.png").resize((S, S), Image.LANCZOS)
    sheet.paste(im, ((i % COLS) * S, (i // COLS) * S))
sheet.save(f"{out}/coin-sheet.webp", "WEBP", quality=88, method=6)
for i, name in ((0, "shill"), (24, "chill"), (12, "edge")):
    Image.open(f"{src}/f{i:03d}.png").resize((256, 256), Image.LANCZOS).save(
        f"{out}/coin-{name}.webp", "WEBP", quality=90
    )

# "Can't tell (yet)": a coin still spinning = premultiplied average of flip frames 4..20
fr = [
    np.asarray(Image.open(f"{src}/f{i:03d}.png").convert("RGBA")).astype(np.float32) / 255
    for i in range(4, 21)
]
acc = np.zeros_like(fr[0])
for f in fr:
    a = f[..., 3:4]
    acc[..., :3] += f[..., :3] * a
    acc[..., 3:4] += a
acc /= len(fr)
rgb = np.where(acc[..., 3:4] > 1e-4, acc[..., :3] / np.maximum(acc[..., 3:4], 1e-4), 0)
alpha = np.clip(acc[..., 3:4] * 1.25, 0, 1)
im = Image.fromarray((np.concatenate([rgb, alpha], -1) * 255).astype(np.uint8), "RGBA")
im.resize((256, 256), Image.LANCZOS).save(f"{out}/coin-unsure.webp", "WEBP", quality=90)
