// Junction-style banner: a darkened Renaissance painting with live machine-vision overlay.
// Keypoints and match scores come from OpenCV ORB run on the painting (assets/venus-cv.json).
import cv from "../assets/venus-cv.json";
import venusUrl from "../assets/venus-banner.webp";

interface Pt { x: number; y: number; s: number; score: number; pin?: boolean; fx?: number; fy?: number } // pin: always shown (covers nudity), showing the picture at fx, fy
const PTS = (cv as { points: Pt[] }).points;
const LINKS = (cv as { links: { a: number; b: number; score: number }[] }).links;

const img = new Image();
img.src = venusUrl;
export const bannerReady = new Promise<void>((res) => {
  if (img.complete && img.naturalWidth) res();
  img.onload = () => res();
  img.onerror = () => res();
});

export interface BannerOpts {
  darken?: number; // 0..1 brightness of the background
  focusX?: number; // 0..1 horizontal focus for cover-cropping
  delay?: number; // ms before the first box
  per?: number; // ms between boxes
  src?: HTMLImageElement; // optional different picture (no keypoints drawn unless `cv` is given)
  cv?: { points: Pt[]; links: { a: number; b: number; score: number }[] }; // keypoints for `src`
  focusY?: number;
  avoid?: () => (Element | null)[]; // keep detections off these elements (text blocks)
}

/** The picture under a box, liquid-warped like the Junction banner: a mild swirl plus a slow ripple.
 * Built once per box; falls back to the plain patch if the canvas cannot be read. */
function warpPatch(pic: HTMLImageElement, p: Pt, i: number): HTMLCanvasElement {
  const nw = pic.naturalWidth;
  const nh = pic.naturalHeight;
  const n = Math.max(16, Math.round(p.s * nw));
  const c = document.createElement("canvas");
  c.width = c.height = n;
  const g = c.getContext("2d")!;
  g.drawImage(pic, (p.fx ?? p.x) * nw - n / 2, (p.fy ?? p.y) * nh - n / 2, n, n, 0, 0, n, n);
  let src: ImageData;
  try {
    src = g.getImageData(0, 0, n, n);
  } catch {
    return c;
  }
  const out = g.createImageData(n, n);
  const turn = (i % 2 ? -1 : 1) * (0.7 + ((i * 0.37) % 0.4));
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const dx = x - n / 2;
      const dy = y - n / 2;
      const r = Math.hypot(dx, dy);
      const a = Math.atan2(dy, dx) + turn * Math.exp(-r / (0.45 * n));
      const sx = Math.min(n - 1, Math.max(0, Math.round(n / 2 + r * Math.cos(a) + 0.03 * n * Math.sin((y / n) * Math.PI * 3))));
      const sy = Math.min(n - 1, Math.max(0, Math.round(n / 2 + r * Math.sin(a) + 0.03 * n * Math.sin((x / n) * Math.PI * 2.4))));
      const o = (y * n + x) * 4;
      const q = (sy * n + sx) * 4;
      out.data[o] = src.data[q];
      out.data[o + 1] = src.data[q + 1];
      out.data[o + 2] = src.data[q + 2];
      out.data[o + 3] = 255;
    }
  }
  g.putImageData(out, 0, 0);
  return c;
}

/** Draws into `canvas` until it leaves the DOM. Returns a stop function. */
export function mountBanner(canvas: HTMLCanvasElement, opts: BannerOpts = {}): () => void {
  const { darken = 0.38, focusX = 0.55, delay = 250, per = 110 } = opts;
  const pic = opts.src ?? img;
  const ctx = canvas.getContext("2d")!;
  const t0 = performance.now();
  let raf = 0;
  let stopped = false;
  const pts0 = opts.cv?.points ?? PTS;
  const links0 = opts.cv?.links ?? LINKS;
  const order = pts0.map((_, i) => i).sort((a, b) => pts0[a].x - pts0[b].x);
  const rank = new Map(order.map((i, r) => [i, r]));
  const patches: HTMLCanvasElement[] = [];

  // keep detections off the text: zones are measured a few times a second, not every frame
  const hidden = new Set<number>();
  const quiet = new Set<number>(); // pins under text: box only, no score over the words
  const offLinks = new Set<number>();
  const avoidDone = new Set<string>();
  let avoidKey = "";
  const draw = (now: number) => {
    if (stopped || !canvas.isConnected) return;
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, devicePixelRatio || 1);
    const W = Math.max(1, Math.round(r.width));
    const H = Math.max(1, Math.round(r.height));
    if (canvas.width !== W * dpr || canvas.height !== H * dpr) {
      canvas.width = W * dpr;
      canvas.height = H * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);
    if (!pic.naturalWidth) {
      raf = requestAnimationFrame(draw);
      return;
    }
    // cover-fit
    const s = Math.max(W / pic.naturalWidth, H / pic.naturalHeight);
    const iw = pic.naturalWidth * s;
    const ih = pic.naturalHeight * s;
    const ix = (W - iw) * focusX;
    const iy = (H - ih) * (opts.focusY ?? 0.3);
    const t = now - t0;
    // slow push-in
    const z = 1 + Math.min(1, t / 14000) * 0.04;
    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.scale(z, z);
    ctx.translate(-W / 2, -H / 2);
    ctx.filter = `brightness(${darken}) saturate(0.85)`;
    ctx.drawImage(pic, ix, iy, iw, ih);
    ctx.filter = "none";
    if (!opts.src || opts.cv) {
      const P = pts0.map((p) => ({ x: ix + p.x * iw, y: iy + p.y * ih, s: p.s * iw, score: p.score, pin: !!p.pin }));
      if (!patches.length) pts0.forEach((p, i) => patches.push(warpPatch(pic, p, i)));
      // no boxes or lines over the text this banner sits behind
      const key = `${W}x${H}:${Math.floor(t / 250)}`;
      if (key !== avoidKey) {
        avoidKey = key;
        hidden.clear();
        quiet.clear();
        offLinks.clear();
      }
      if (opts.avoid && !avoidDone.has(key)) {
        avoidDone.clear();
        avoidDone.add(key);
        const pad = 14;
        const zones = opts.avoid()
          .filter((el): el is Element => !!el)
          .map((el) => el.getBoundingClientRect())
          .filter((q) => q.width && q.height)
          .map((q) => ({ x0: q.left - r.left - pad, y0: q.top - r.top - pad, x1: q.right - r.left + pad, y1: q.bottom - r.top + pad }));
        const sx = (x: number) => W / 2 + (x - W / 2) * z;
        const sy = (y: number) => H / 2 + (y - H / 2) * z;
        P.forEach((p, i) => {
          const h = (p.s * (p.pin ? 1 : 1.8) * z) / 2; // pins stay put: only their own box counts
          const x = sx(p.x);
          const y = sy(p.y);
          if (zones.some((q) => x + h > q.x0 && x - h < q.x1 && y + h > q.y0 && y - h < q.y1)) (p.pin ? quiet : hidden).add(i);
        });
        const crosses = (a: { x: number; y: number }, b: { x: number; y: number }) =>
          zones.some((q) => {
            for (let k = 0; k <= 24; k++) {
              const x = sx(a.x + ((b.x - a.x) * k) / 24);
              const y = sy(a.y + ((b.y - a.y) * k) / 24);
              if (x > q.x0 && x < q.x1 && y > q.y0 && y < q.y1) return true;
            }
            return false;
          });
        links0.forEach((l, li) => {
          if (!hidden.has(l.a) && !hidden.has(l.b) && crosses(P[l.a], P[l.b])) offLinks.add(li);
        });
      }
      // match lines draw in after both ends are detected
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(235,235,235,0.75)";
      ctx.setLineDash([6, 6]);
      for (const [li, l] of links0.entries()) {
        const ta = delay + (rank.get(l.a) ?? 0) * per;
        const tb = delay + (rank.get(l.b) ?? 0) * per;
        const p = Math.min(1, Math.max(0, (t - Math.max(ta, tb) - 150) / 600));
        if (p <= 0 || hidden.has(l.a) || hidden.has(l.b) || offLinks.has(li)) continue;
        const a = P[l.a];
        const b = P[l.b];
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(a.x + (b.x - a.x) * p, a.y + (b.y - a.y) * p);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      // detection boxes: bright patch, green frame, score ticking up to the real value
      P.forEach((p, i) => {
        const tt = p.pin ? t : t - delay - (rank.get(i) ?? 0) * per; // pins are there from the first frame
        if (tt < 0 || hidden.has(i)) return;
        const e = Math.min(1, tt / 260);
        const grow = (1 - e) * p.s * 0.8;
        const sz = p.s + grow;
        const x = p.x - sz / 2;
        const y = p.y - sz / 2;
        ctx.globalAlpha = p.pin ? 1 : e;
        ctx.drawImage(patches[i], x, y, sz, sz);
        ctx.globalAlpha = e;
        ctx.strokeStyle = "#3fe07a";
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, sz, sz);
        if (quiet.has(i)) {
          ctx.globalAlpha = 1;
          return;
        }
        const shown = p.score * Math.min(1, tt / 520);
        ctx.font = `600 ${Math.max(9, Math.min(12, sz / 5))}px "JetBrains Mono", monospace`;
        ctx.fillStyle = "#fff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.shadowColor = "#000";
        ctx.shadowBlur = 4;
        ctx.fillText(shown.toFixed(4), p.x, p.y);
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 1;
      });
    }
    ctx.restore();
    raf = requestAnimationFrame(draw);
  };
  raf = requestAnimationFrame(draw);
  return () => {
    stopped = true;
    cancelAnimationFrame(raf);
  };
}
