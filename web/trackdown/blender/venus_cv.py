# Junction-style banner data: ORB keypoints + descriptor-match scores on Botticelli's Birth of Venus.
# Run from a folder holding venus.jpg (Wikimedia Commons, public domain) with opencv-python + numpy;
# writes out/venus-banner.jpg and out/venus-cv.json (copy to src/assets/).
import json
import cv2
import numpy as np

img = cv2.imread("venus.jpg")  # 1920x1206
crop = img[0:300, :]  # heads and shoulders band, like Junction's banner
cv2.imwrite("out/venus-banner.jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
h, w = crop.shape[:2]
gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
orb = cv2.ORB_create(nfeatures=3000, scaleFactor=1.3, nlevels=6, edgeThreshold=20)
kps, des = orb.detectAndCompute(gray, None)
# non-max suppression on a coarse grid: strongest keypoint per cell
cell = 170
best = {}
for i, k in enumerate(kps):
    key = (int(k.pt[0] // cell), int(k.pt[1] // cell))
    if key not in best or k.response > kps[best[key]].response:
        best[key] = i
idx = sorted(best.values(), key=lambda i: -kps[i].response)[:22]
resp = np.array([kps[i].response for i in idx])
pts = []
for j, i in enumerate(idx):
    k = kps[i]
    size = float(np.clip(k.size * 1.6, 46, 120))
    pts.append({"x": round(k.pt[0] / w, 4), "y": round(k.pt[1] / h, 4), "s": round(size / w, 4), "r": float(k.response)})
# matches between selected keypoints by descriptor distance (self-similarity in the painting)
D = des[idx]
pairs = []
for a in range(len(idx)):
    for b in range(a + 1, len(idx)):
        d = cv2.norm(D[a], D[b], cv2.NORM_HAMMING)
        pairs.append((d, a, b))
pairs.sort()
used = {}
links = []
for d, a, b in pairs:
    if used.get(a, 0) >= 2 or used.get(b, 0) >= 2:
        continue
    links.append({"a": a, "b": b, "score": round(1 - d / 256, 4)})
    used[a] = used.get(a, 0) + 1
    used[b] = used.get(b, 0) + 1
    if len(links) >= 16:
        break
# box label = best match score of that keypoint (what the banner shows)
for j, p in enumerate(pts):
    sc = [l["score"] for l in links if l["a"] == j or l["b"] == j]
    p["score"] = max(sc) if sc else round(float(p["r"] / resp.max()), 4)
json.dump({"w": w, "h": h, "points": pts, "links": links}, open("out/venus-cv.json", "w"), indent=0)
# preview with overlay
dark = (crop * 0.42).astype(np.uint8)
for l in links:
    a, b = pts[l["a"]], pts[l["b"]]
    p1 = (int(a["x"] * w), int(a["y"] * h)); p2 = (int(b["x"] * w), int(b["y"] * h))
    n = int(np.hypot(p2[0] - p1[0], p2[1] - p1[1]) // 12)
    for t in range(n):
        if t % 2 == 0:
            q1 = (int(p1[0] + (p2[0] - p1[0]) * t / n), int(p1[1] + (p2[1] - p1[1]) * t / n))
            q2 = (int(p1[0] + (p2[0] - p1[0]) * (t + 1) / n), int(p1[1] + (p2[1] - p1[1]) * (t + 1) / n))
            cv2.line(dark, q1, q2, (220, 220, 220), 1, cv2.LINE_AA)
for p in pts:
    s = int(p["s"] * w); x = int(p["x"] * w - s / 2); y = int(p["y"] * h - s / 2)
    x0, y0 = max(0, x), max(0, y)
    dark[y0:y + s, x0:x + s] = crop[y0:y + s, x0:x + s]
    cv2.rectangle(dark, (x, y), (x + s, y + s), (60, 220, 60), 2)
    cv2.putText(dark, f"{p['score']:.4f}", (x + 4, y + s // 2 + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (235, 235, 235), 1, cv2.LINE_AA)
cv2.imwrite("out/prev-cv.png", cv2.resize(dark, (1440, int(1440 * h / w))))
print(len(pts), "points", len(links), "links")
