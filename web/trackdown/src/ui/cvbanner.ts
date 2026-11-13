// Junction-style banner: a darkened Renaissance painting with live machine-vision overlay.
// Keypoints and match scores come from OpenCV ORB run on the painting (assets/venus-cv.json).
import cv from "../assets/venus-cv.json";
import venusUrl from "../assets/venus-banner.webp";

interface Pt { x: number; y: number; s: number; score: number }
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
      const P = pts0.map((p) => ({ x: ix + p.x * iw, y: iy + p.y * ih, s: p.s * iw, score: p.score }));
      // match lines draw in after both ends are detected
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(235,235,235,0.75)";
      ctx.setLineDash([6, 6]);
      for (const l of links0) {
        const ta = delay + (rank.get(l.a) ?? 0) * per;
        const tb = delay + (rank.get(l.b) ?? 0) * per;
        const p = Math.min(1, Math.max(0, (t - Math.max(ta, tb) - 150) / 600));
        if (p <= 0) continue;
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
        const tt = t - delay - (rank.get(i) ?? 0) * per;
        if (tt < 0) return;
        const e = Math.min(1, tt / 260);
        const grow = (1 - e) * p.s * 0.8;
        const sz = p.s + grow;
        const x = p.x - sz / 2;
        const y = p.y - sz / 2;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, sz, sz);
        ctx.clip();
        ctx.globalAlpha = e;
        ctx.drawImage(pic, ix, iy, iw, ih);
        ctx.restore();
        ctx.globalAlpha = e;
        ctx.strokeStyle = "#3fe07a";
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, sz, sz);
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
