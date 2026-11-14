// Timeline dock (scrubber + replay), time ruler and minimap.
import type { Graph } from "../engine/graph";
import { fmtClock, fmtDate, fmtDur, fmtShort, ROLE } from "./format";
import { icon } from "./icons";

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
// pigments lifted for the black chrome strip
const ON_BLACK: Record<string, string> = { origin: "#e4b36a", mutation: "#d9783d", relay: "#8f8b84", amplifier: "#e2533c", organic: "#5fc9a8", submitted: "#f2efe9", source: "#7aa2f0" };

function fitCanvas(c: HTMLCanvasElement) {
  const r = c.getBoundingClientRect();
  const dpr = Math.min(2, devicePixelRatio || 1);
  const w = Math.round(r.width * dpr);
  const h = Math.round(r.height * dpr);
  if (c.width !== w || c.height !== h) {
    c.width = w;
    c.height = h;
  }
  const ctx = c.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: r.width, h: r.height };
}

export class Dock {
  g: Graph;
  el: HTMLElement;
  canvas: HTMLCanvasElement;
  playBtn: HTMLButtonElement;
  clock: HTMLElement;
  u = 1;
  playing = false;
  speed = 1;
  private dragging = false;
  private last = 0;
  onScrub: () => void = () => {};
  constructor(el: HTMLElement, g: Graph) {
    this.g = g;
    this.el = el;
    el.innerHTML = `<div class="dock-left"><button class="play" aria-label="Play">${icon.play}</button><div class="clock"><div class="d">–</div><div class="rel">–</div></div></div>
      <div class="scrub"><canvas></canvas></div>
      <div class="dock-right"><button class="speed" data-speed>1×</button><button class="speed" data-end title="Show everything">All</button></div>`;
    this.canvas = el.querySelector("canvas")!;
    this.playBtn = el.querySelector(".play")!;
    this.clock = el.querySelector(".clock")!;
    this.playBtn.onclick = () => this.toggle();
    el.querySelector<HTMLButtonElement>("[data-speed]")!.onclick = (e) => {
      this.speed = this.speed === 1 ? 3 : this.speed === 3 ? 0.4 : 1;
      (e.currentTarget as HTMLElement).textContent = `${this.speed}×`;
    };
    el.querySelector<HTMLButtonElement>("[data-end]")!.onclick = () => this.showAll();
    const scrub = el.querySelector<HTMLElement>(".scrub")!;
    const at = (e: PointerEvent) => {
      const r = scrub.getBoundingClientRect();
      this.seek(clamp((e.clientX - r.left - 8) / (r.width - 16), 0, 1));
    };
    scrub.addEventListener("pointerdown", (e) => {
      this.dragging = true;
      scrub.setPointerCapture(e.pointerId);
      this.pause();
      at(e);
    });
    scrub.addEventListener("pointermove", (e) => this.dragging && at(e));
    scrub.addEventListener("pointerup", () => (this.dragging = false));
  }

  private range() {
    const m = this.g.timeMap;
    return m.length ? { x0: m[0][1], x1: m[m.length - 1][1] } : { x0: 0, x1: 1 };
  }
  /** inverse of graph.xForTime */
  timeForU(u: number): number {
    const m = this.g.timeMap;
    if (!m.length) return 0;
    const { x0, x1 } = this.range();
    const x = x0 + u * (x1 - x0);
    for (let i = 1; i < m.length; i++) {
      if (x <= m[i][1]) {
        const [ta, xa] = m[i - 1];
        const [tb, xb] = m[i];
        return xb === xa ? tb : ta + ((x - xa) / (xb - xa)) * (tb - ta);
      }
    }
    return m[m.length - 1][0];
  }
  uForTime(t: number) {
    const { x0, x1 } = this.range();
    return x1 === x0 ? 1 : (this.g.xForTime(t) - x0) / (x1 - x0);
  }

  seek(u: number) {
    this.u = u;
    this.g.revealed = null;
    this.g.setNow(u >= 0.9999 ? Infinity : this.timeForU(u));
    this.onScrub();
  }
  showAll() {
    this.pause();
    this.u = 1;
    this.g.revealAll();
    this.onScrub();
  }
  play() {
    if (this.u >= 0.999) this.seek(0);
    this.playing = true;
    this.last = performance.now();
    this.playBtn.innerHTML = icon.pause;
  }
  pause() {
    this.playing = false;
    this.playBtn.innerHTML = icon.play;
  }
  toggle() {
    this.playing ? this.pause() : this.play();
  }

  tick(now: number) {
    if (this.playing) {
      const dt = (now - this.last) / 1000;
      this.last = now;
      const nu = this.u + (dt / 16) * this.speed;
      if (nu >= 1) {
        this.seek(1);
        this.pause();
      } else this.seek(nu);
    }
    this.draw();
  }

  draw() {
    const { ctx: c, w, h } = fitCanvas(this.canvas);
    c.clearRect(0, 0, w, h);
    const g = this.g;
    if (!g.case) return;
    const L = 8;
    const R = w - 8;
    const base = h - 16;
    const top = 6;
    const { x0, x1 } = this.range();
    const ux = (x: number) => L + ((x - x0) / Math.max(1, x1 - x0)) * (R - L);
    // bins
    const bins = Math.max(20, Math.floor((R - L) / 5));
    const counts: { n: number; roles: Record<string, number> }[] = Array.from({ length: bins }, () => ({ n: 0, roles: {} }));
    for (const n of g.nodes) {
      const b = clamp(Math.floor(((n.tx - x0) / Math.max(1, x1 - x0)) * (bins - 1)), 0, bins - 1);
      counts[b].n++;
      counts[b].roles[n.d.role] = (counts[b].roles[n.d.role] ?? 0) + 1;
    }
    const max = Math.max(...counts.map((b) => b.n), 1);
    const bw = (R - L) / bins;
    const playX = L + this.u * (R - L);
    counts.forEach((b, i) => {
      if (!b.n) return;
      const x = L + i * bw;
      const hh = Math.max(3, Math.sqrt(b.n / max) * (base - top));
      let y = base;
      const order = ["submitted", "origin", "mutation", "source", "relay", "organic", "amplifier"];
      for (const r of order) {
        const cnt = b.roles[r];
        if (!cnt) continue;
        const sh = (cnt / b.n) * hh;
        c.fillStyle = ON_BLACK[r];
        c.globalAlpha = x > playX ? 0.2 : 0.95;
        c.fillRect(x + 0.5, y - sh, Math.max(1.5, bw - 1.5), sh);
        y -= sh;
      }
    });
    c.globalAlpha = 1;
    // axis
    c.strokeStyle = "rgba(255,255,255,0.16)";
    c.beginPath();
    c.moveTo(L, base + 0.5);
    c.lineTo(R, base + 0.5);
    c.stroke();
    // break + endpoint labels
    c.font = '500 10.5px "JetBrains Mono", monospace';
    c.fillStyle = "#908c84";
    c.textBaseline = "top";
    const first = g.timeMap[0]?.[0];
    const last = g.timeMap[g.timeMap.length - 1]?.[0];
    if (first != null) {
      // under a day the dates would be identical: show clock times instead
      const short = last - first < 86400000;
      const end = (t: number) => (short ? fmtClock(t).slice(0, 5) : fmtShort(t));
      const a = end(first);
      const b = end(last);
      // narrow dock: keep the end labels apart, or show only the latest
      const fits = c.measureText(a).width + c.measureText(b).width + 16 < R - L;
      c.textAlign = "right";
      c.fillText(b, R, base + 4);
      if (fits) {
        c.textAlign = "left";
        c.fillText(a, L, base + 4);
      }
    }
    c.textAlign = "center";
    let lastB = -1e9;
    for (const b of g.timeBreaks) {
      const x = ux(b.x);
      if (x < L + 80 || x > R - 95 || x - lastB < 80) continue;
      lastB = x;
      c.fillStyle = "#e4b36a";
      c.fillText(`${fmtDur(b.gap)} gap`, x, base + 4);
    }
    // origin + submitted markers
    const mark = (id: string, col: string) => {
      const n = g.byId.get(id);
      if (!n) return;
      const x = ux(n.tx);
      c.fillStyle = col;
      c.beginPath();
      c.moveTo(x, top - 2);
      c.lineTo(x + 4, top + 3);
      c.lineTo(x, top + 8);
      c.lineTo(x - 4, top + 3);
      c.fill();
    };
    mark(g.case.originId, ON_BLACK.origin);
    mark(g.case.submittedId, "#f2efe9");
    // playhead
    c.fillStyle = "#3fe07a";
    c.shadowColor = "#3fe07a";
    c.shadowBlur = 8;
    c.fillRect(playX - 0.5, top - 4, 1, base - top + 8);
    c.shadowBlur = 0;
    c.beginPath();
    c.moveTo(playX - 5, top - 6);
    c.lineTo(playX + 5, top - 6);
    c.lineTo(playX, top);
    c.fill();
    // clock
    const t = this.u >= 0.9999 ? last : this.timeForU(this.u);
    if (t != null && first != null) {
      const d = this.clock.querySelector(".d")!;
      const rel = this.clock.querySelector(".rel")!;
      const txt = `${fmtDate(t, false)} ${fmtClock(t)}`;
      if (d.textContent !== txt) d.textContent = txt;
      const r2 = t - first < 1000 ? "Earliest copy" : `${fmtDur(t - first)} after the earliest copy`;
      if (rel.textContent !== r2) rel.textContent = r2;
    }
  }
}

/** Date labels across the top of the stage, aligned with the time axis. */
export function drawRuler(canvas: HTMLCanvasElement, g: Graph) {
  const { ctx: c, w, h } = fitCanvas(canvas);
  c.clearRect(0, 0, w, h);
  if (g.mode !== "timeline" || !g.case) return;
  const ns = g.nodes.filter((n) => n.vis && !n.d.undated).sort((a, b) => a.tx - b.tx);
  c.font = '600 11px "JetBrains Mono", monospace';
  c.textBaseline = "middle";
  c.textAlign = "left";
  let lastX = -1e9;
  let lastT = -1e15;
  let lastLabel = "";
  let firstDone = false;
  const drawn: [number, number][] = []; // x-extents of date labels; "later" pills keep off them
  for (const n of ns) {
    const s = g.toScreen(n.tx, 0);
    if (s.x < -20 || s.x > w + 20) continue;
    c.fillStyle = "rgba(255,255,255,0.3)";
    c.fillRect(s.x, h - 5, 1, 5);
    if (s.x - lastX < 120) continue;
    // the first visible label always carries the date; later ones say only what changed
    if (firstDone && g.timeBreaks.some((b) => Math.abs(g.toScreen(b.x, 0).x - s.x - 40) < 95)) continue;
    const newDay = !firstDone || new Date(n.d.t).toDateString() !== new Date(lastT).toDateString();
    const label = newDay ? fmtDate(n.d.t, false) : fmtClock(n.d.t).slice(0, 5);
    if (label === lastLabel) continue;
    if (s.x + 5 + c.measureText(label.toUpperCase()).width > w - 6) continue; // never clip at the edge
    lastLabel = label;
    c.fillStyle = "rgba(255,255,255,0.6)";
    c.fillRect(s.x, h - 12, 1, 12);
    c.fillStyle = "#f2efe9";
    c.fillText(label.toUpperCase(), s.x + 5, h / 2 - 1);
    drawn.push([s.x, s.x + 5 + c.measureText(label.toUpperCase()).width]);
    lastX = s.x;
    lastT = n.d.t;
    firstDone = true;
  }
  c.textAlign = "center";
  let lastPill = -1e9;
  for (const b of g.timeBreaks) {
    const s = g.toScreen(b.x, 0);
    if (s.x < 0 || s.x > w || s.x - lastPill < 100) continue;
    const label = `${fmtDur(b.gap)} later`;
    c.font = '600 10px "JetBrains Mono", monospace';
    const up = label.toUpperCase();
    const tw = c.measureText(up).width + 16;
    if (s.x - tw / 2 < 2 || s.x + tw / 2 > w - 2) continue;
    if (drawn.some(([a, b]) => s.x + tw / 2 + 6 > a && s.x - tw / 2 - 6 < b)) continue;
    lastPill = s.x;
    c.fillStyle = "#050506";
    c.fillRect(s.x - tw / 2, h / 2 - 10, tw, 18);
    c.setLineDash([3, 3]);
    c.strokeStyle = "rgba(255,255,255,0.55)";
    c.strokeRect(s.x - tw / 2 + 0.5, h / 2 - 9.5, tw - 1, 17);
    c.setLineDash([]);
    c.fillStyle = "#f2efe9";
    c.fillText(up, s.x, h / 2);
    c.font = '600 11px "JetBrains Mono", monospace';
  }
}

export class Minimap {
  g: Graph;
  canvas: HTMLCanvasElement;
  private b = { x0: 0, y0: 0, s: 1, ox: 0, oy: 0 };
  constructor(el: HTMLElement, g: Graph) {
    this.g = g;
    this.canvas = document.createElement("canvas");
    el.appendChild(this.canvas);
    let down = false;
    const go = (e: PointerEvent) => {
      const r = this.canvas.getBoundingClientRect();
      const x = (e.clientX - r.left - this.b.ox) / this.b.s + this.b.x0;
      const y = (e.clientY - r.top - this.b.oy) / this.b.s + this.b.y0;
      if (down && e.type === "pointermove") {
        g.cx = x;
        g.cy = y;
      } else g.flyTo(x, y, g.k, 500);
    };
    this.canvas.addEventListener("pointerdown", (e) => {
      down = true;
      this.canvas.setPointerCapture(e.pointerId);
      go(e);
    });
    this.canvas.addEventListener("pointermove", (e) => down && go(e));
    this.canvas.addEventListener("pointerup", () => (down = false));
  }
  draw() {
    const { ctx: c, w, h } = fitCanvas(this.canvas);
    c.clearRect(0, 0, w, h);
    const ns = this.g.nodes.filter((n) => n.vis);
    if (!ns.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) {
      x0 = Math.min(x0, n.x - n.w / 2);
      x1 = Math.max(x1, n.x + n.w / 2);
      y0 = Math.min(y0, n.y - n.h / 2);
      y1 = Math.max(y1, n.y + n.h / 2);
    }
    const pad = 14;
    const s = Math.min((w - pad * 2) / Math.max(x1 - x0, 1600), (h - pad * 2 - 10) / Math.max(y1 - y0, 900));
    const ox = (w - (x1 - x0) * s) / 2;
    const oy = (h - (y1 - y0) * s) / 2 + 6;
    this.b = { x0, y0, s, ox, oy };
    for (const n of ns) {
      c.fillStyle = ROLE[n.d.role].color;
      c.globalAlpha = 0.85;
      const rw = Math.max(2, n.w * s);
      const rh = Math.max(1.5, n.h * s);
      c.fillRect(ox + (n.x - n.w / 2 - x0) * s, oy + (n.y - n.h / 2 - y0) * s, rw, rh);
    }
    c.globalAlpha = 1;
    const g = this.g;
    const tl = g.toWorld(0, 0);
    const br = g.toWorld(g.W, g.H);
    c.strokeStyle = "#3fe07a";
    c.lineWidth = 1;
    c.fillStyle = "rgba(63,224,122,0.07)";
    const rx = ox + (tl.x - x0) * s;
    const ry = oy + (tl.y - y0) * s;
    const rw = (br.x - tl.x) * s;
    const rh = (br.y - tl.y) * s;
    c.fillRect(rx, ry, rw, rh);
    c.strokeRect(rx + 0.5, ry + 0.5, rw, rh);
  }
}
