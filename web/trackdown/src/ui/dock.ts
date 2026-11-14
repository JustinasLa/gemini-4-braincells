// Timeline dock (scrubber + replay), time ruler and minimap.
import type { Graph, VNode } from "../engine/graph";
import { esc, fmtClock, fmtDate, fmtDur, fmtShort, MONO, roleOf, SANS } from "./format";
import { icon } from "./icons";

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
// pigments lifted for the black chrome strip
const ON_BLACK: Record<string, string> = { origin: "#e4b36a", mutation: "#d9783d", relay: "#8f8b84", amplifier: "#e2533c", organic: "#5fc9a8", submitted: "#f2efe9", source: "#7aa2f0" };
// stacking order inside a column, bottom up: the posts the story is about sit on the axis
const ORDER = ["submitted", "origin", "mutation", "source", "relay", "organic", "amplifier"];
const rank = (r: string) => (ORDER.includes(r) ? ORDER.indexOf(r) : ORDER.indexOf("relay"));
const pigment = (r: string) => (Object.prototype.hasOwnProperty.call(ON_BLACK, r) ? ON_BLACK[r] : ON_BLACK.relay);
const MIN = 60000;
const HOUR = 3600000;
const DAY = 86400000;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const COL_W = 7; // px per column of post marks on the strip

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

/** Calendar ticks between two times, at the finest unit that keeps the count sane. The strip's
 *  time axis is not linear (long quiet gaps are squeezed), so labels are thinned by pixel distance later. */
export function calendarTicks(t0: number, t1: number): { t: number; label: string; major: boolean }[] {
  const out: { t: number; label: string; major: boolean }[] = [];
  const span = t1 - t0;
  const day = (t: number) => {
    const d = new Date(t);
    return `${d.getUTCDate()} ${MON[d.getUTCMonth()]}`;
  };
  const hm = (t: number) => fmtClock(t).slice(0, 5);
  if (span <= 400 * DAY && span >= 2 * DAY) {
    for (let t = Math.ceil(t0 / DAY) * DAY; t <= t1; t += DAY) {
      const d = new Date(t);
      out.push({ t, label: d.getUTCDate() === 1 ? `${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}` : day(t), major: d.getUTCDate() === 1 });
    }
  } else if (span < 2 * DAY) {
    const step = span < 20 * MIN ? MIN : span < 3 * HOUR ? 10 * MIN : HOUR;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      const midnight = t % DAY === 0;
      out.push({ t, label: midnight ? day(t) : hm(t), major: midnight });
    }
  } else {
    // months, or years past ~30 years
    const d0 = new Date(t0);
    const byYear = span > 30 * 365 * DAY;
    let y = d0.getUTCFullYear();
    let m = byYear ? 0 : d0.getUTCMonth() + 1;
    if (byYear) y++;
    for (;;) {
      if (m > 11) (m = 0), y++;
      const t = Date.UTC(y, m, 1);
      if (t > t1) break;
      out.push({ t, label: m === 0 ? String(y) : MON[m], major: m === 0 });
      if (byYear) y++;
      else m++;
    }
  }
  return out;
}

/** posts close enough on the strip to share one column of marks */
interface Col {
  x: number;
  ns: VNode[]; // bottom-up stacking order
  t0: number;
  t1: number;
  top: number; // y of the top of the stack, after drawing
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
  private downX = 0;
  private slider!: HTMLInputElement;
  private tip!: HTMLElement;
  private scrub!: HTMLElement;
  private last = 0;
  private cols: Col[] = [];
  private hoverX: number | null = null; // css px inside the canvas
  private hoverCol: Col | null = null;
  private tickCache: { key: string; ticks: ReturnType<typeof calendarTicks> } = { key: "", ticks: [] };
  onScrub: () => void = () => {};
  constructor(el: HTMLElement, g: Graph) {
    this.g = g;
    this.el = el;
    el.innerHTML = `<div class="dock-left">
        <div class="transport">
          <button class="step" data-step="-1" aria-label="Previous post" title="Previous post  ( , )">${icon.arrowL}</button>
          <button class="play" aria-label="Play" title="Play / pause  (space)">${icon.play}</button>
          <button class="step" data-step="1" aria-label="Next post" title="Next post  ( . )">${icon.arrowR}</button>
        </div>
        <div class="clock"><div class="d">–</div><div class="rel">–</div></div>
      </div>
      <div class="scrub"><canvas></canvas><input class="scrub-range" type="range" min="0" max="1000" step="1" value="1000" aria-label="Timeline position" /><div class="scrub-tip" role="presentation"></div></div>
      <div class="dock-right"><button class="speed" data-speed title="Playback speed">Speed 1×</button><button class="speed" data-end title="Jump to the end and show every post">Show all</button></div>`;
    this.canvas = el.querySelector("canvas")!;
    this.playBtn = el.querySelector(".play")!;
    this.clock = el.querySelector(".clock")!;
    this.tip = el.querySelector(".scrub-tip")!;
    this.playBtn.onclick = () => this.toggle();
    el.querySelectorAll<HTMLButtonElement>("[data-step]").forEach((b) => (b.onclick = () => this.step(Number(b.dataset.step) as 1 | -1)));
    el.querySelector<HTMLButtonElement>("[data-speed]")!.onclick = (e) => {
      const SPEEDS = [1, 2, 4, 0.5];
      this.speed = SPEEDS[(SPEEDS.indexOf(this.speed) + 1) % SPEEDS.length];
      (e.currentTarget as HTMLElement).textContent = `Speed ${this.speed}×`;
    };
    el.querySelector<HTMLButtonElement>("[data-end]")!.onclick = () => this.showAll();
    const scrub = (this.scrub = el.querySelector<HTMLElement>(".scrub")!);
    // keyboard access to the same scrubbing (the canvas handles the pointer): arrows step post by post
    this.slider = el.querySelector<HTMLInputElement>(".scrub-range")!;
    this.slider.addEventListener("input", () => {
      this.pause();
      this.seek(Number(this.slider.value) / 1000);
    });
    this.slider.addEventListener("keydown", (e) => {
      const k = e.key;
      if (k === "ArrowLeft" || k === "ArrowDown") this.step(-1);
      else if (k === "ArrowRight" || k === "ArrowUp") this.step(1);
      else if (k === "Home") (this.pause(), this.seek(0));
      else if (k === "End") this.showAll();
      else return;
      e.preventDefault();
    });
    const localX = (e: PointerEvent) => e.clientX - this.canvas.getBoundingClientRect().left;
    const at = (e: PointerEvent) => {
      const w = this.canvas.getBoundingClientRect().width;
      this.seek(clamp((localX(e) - 8) / (w - 16), 0, 1));
    };
    scrub.addEventListener("pointerdown", (e) => {
      this.dragging = true;
      this.downX = e.clientX;
      scrub.setPointerCapture(e.pointerId);
      this.pause();
      this.hover(localX(e), e.pointerType);
      at(e);
    });
    scrub.addEventListener("pointermove", (e) => {
      if (this.dragging) at(e);
      if (this.dragging || e.pointerType === "mouse") this.hover(localX(e), e.pointerType);
    });
    const up = (e: PointerEvent) => {
      if (!this.dragging) return;
      this.dragging = false;
      // a click (not a drag) on a column of posts jumps to them and opens the post
      if (e.type === "pointerup" && Math.abs(e.clientX - this.downX) < 5) {
        const col = this.colAt(localX(e), e.pointerType);
        if (col) this.jumpTo(col);
      }
      if (e.pointerType !== "mouse") this.hover(null);
    };
    scrub.addEventListener("pointerup", up);
    scrub.addEventListener("pointercancel", up);
    scrub.addEventListener("pointerleave", () => !this.dragging && this.hover(null));
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
  /** the posts the strip counts: the real post shown in a screenshot is not part of the spread */
  private posts() {
    return this.g.nodes.filter((n) => n.d.role !== "screenshotted");
  }

  seek(u: number) {
    this.u = u;
    this.g.revealed = null;
    this.g.setNow(u >= 0.9999 ? Infinity : this.timeForU(u));
    this.onScrub();
  }
  /** move the playhead to an exact moment, so a post at that time is shown (u alone can round just short of it) */
  seekTime(t: number) {
    const m = this.g.timeMap;
    if (!m.length || t >= m[m.length - 1][0]) return this.seek(1);
    this.u = clamp(this.uForTime(t), 0, 0.9998);
    this.g.revealed = null;
    this.g.setNow(t);
    this.onScrub();
  }
  showAll() {
    this.pause();
    this.u = 1;
    this.g.revealAll();
    this.onScrub();
  }
  /** step the playhead to the previous / next moment a post appeared, and bring that post into view */
  step(dir: 1 | -1) {
    this.pause();
    const posts = this.posts().filter((n) => !n.d.undated);
    const ts = [...new Set(posts.map((n) => n.d.t))].sort((a, b) => a - b);
    if (!ts.length) return;
    const now = this.u >= 0.9999 || !Number.isFinite(this.g.now) ? Infinity : this.g.now;
    let t: number | undefined;
    if (dir > 0) t = ts.find((x) => x > now + 500);
    else {
      const ref = Number.isFinite(now) ? now : ts[ts.length - 1];
      t = [...ts].reverse().find((x) => x < ref - 500) ?? ts[0];
    }
    if (t == null) return this.showAll();
    this.seekTime(t);
    const n = posts.filter((p) => p.d.t === t).sort((a, b) => rank(a.d.role) - rank(b.d.role))[0];
    if (n) this.g.focus(n.d.id, this.g.k, 500);
  }
  private jumpTo(col: Col) {
    this.seekTime(col.t1);
    const n = col.ns[0];
    this.g.select(n.d.id);
    this.g.focus(n.d.id, Math.max(this.g.k, 0.9), 600);
  }
  play() {
    if (this.u >= 0.999) this.seek(0);
    this.playing = true;
    this.last = performance.now();
    this.playBtn.innerHTML = icon.pause;
    this.playBtn.setAttribute("aria-label", "Pause");
  }
  pause() {
    this.playing = false;
    this.playBtn.innerHTML = icon.play;
    this.playBtn.setAttribute("aria-label", "Play");
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

  private colAt(x: number | null, pointerType = "mouse"): Col | null {
    if (x == null) return null;
    const reach = pointerType === "mouse" ? 6 : 14;
    let best: Col | null = null;
    for (const c of this.cols) if (Math.abs(c.x - x) <= reach && (!best || Math.abs(c.x - x) < Math.abs(best.x - x))) best = c;
    return best;
  }

  /** what is under the pointer: the posts in that column, or the moment the playhead would move to */
  private hover(x: number | null, pointerType = "mouse") {
    this.hoverX = x;
    this.hoverCol = this.colAt(x, pointerType);
    const tip = this.tip;
    if (x == null || !this.g.case || !this.g.timeMap.length) {
      tip.classList.remove("on");
      return;
    }
    const w = this.canvas.getBoundingClientRect().width;
    const col = this.hoverCol;
    let html: string;
    if (col) {
      const sameDay = new Date(col.t0).toDateString() === new Date(col.t1).toDateString();
      const when = col.t0 === col.t1 ? `${fmtDate(col.t0, false)} · ${fmtClock(col.t0)}` : sameDay ? `${fmtDate(col.t0, false)} · ${fmtClock(col.t0).slice(0, 5)}–${fmtClock(col.t1).slice(0, 5)}` : `${fmtShort(col.t0)} – ${fmtShort(col.t1)}`;
      const shown = col.ns.slice(0, 5);
      const rows = shown
        .map((n) => {
          const r = roleOf(n.d.role);
          const time = n.d.undated || n.d.timeUnknown ? "date unknown" : fmtClock(n.d.t).slice(0, 5);
          return `<div class="st-row"><i style="background:${pigment(n.d.role)}"></i><b>${esc(n.d.author.name)}</b><span>${esc(r.label)} · ${time}</span></div>`;
        })
        .join("");
      const more = col.ns.length > shown.length ? `<div class="st-more">+ ${col.ns.length - shown.length} more</div>` : "";
      html = `<div class="st-h">${col.ns.length === 1 ? "1 post" : `${col.ns.length} posts`} · ${when}</div>${rows}${more}<div class="st-hint">${this.dragging ? "Release to open" : "Click to jump here and open"}</div>`;
    } else {
      const u = clamp((x - 8) / (w - 16), 0, 1);
      const t = this.timeForU(u);
      const gap = this.g.timeBreaks.find((b) => Math.abs(this.uxForWorld(b.x, w) - x) < 18);
      html = `<div class="st-h">${fmtDate(t, false)} · ${fmtClock(t).slice(0, 5)}</div>${gap ? `<div class="st-more">${fmtDur(gap.gap)} with no posts (squeezed)</div>` : ""}<div class="st-hint">${this.dragging ? "Dragging the playhead" : "Click or drag to move the playhead"}</div>`;
    }
    if (tip.innerHTML !== html) tip.innerHTML = html;
    tip.classList.add("on");
    const tw = tip.offsetWidth;
    const sw = this.scrub.getBoundingClientRect().width;
    const cx = col ? col.x : x;
    tip.style.left = `${clamp(cx - tw / 2, -4, sw - tw + 4)}px`;
  }
  private uxForWorld(wx: number, w: number) {
    const { x0, x1 } = this.range();
    return 8 + ((wx - x0) / Math.max(1, x1 - x0)) * (w - 16);
  }

  draw() {
    const { ctx: c, w, h } = fitCanvas(this.canvas);
    c.clearRect(0, 0, w, h);
    const g = this.g;
    if (!g.case) return;
    const L = 8;
    const R = w - 8;
    const base = h - 17; // axis
    const flagY = 9; // labels for the earliest and the submitted post
    const markTop = 15;
    const ux = (x: number) => this.uxForWorld(x, w);
    const first = g.timeMap[0]?.[0];
    const last = g.timeMap[g.timeMap.length - 1]?.[0];
    const playX = L + this.u * (R - L);

    // ---- columns of post marks: one square per post, stacked, so a burst reads as a tall column
    const byK = new Map<number, Col>();
    for (const n of this.posts()) {
      const k = Math.round((ux(n.tx) - L) / COL_W);
      let col = byK.get(k);
      if (!col) byK.set(k, (col = { x: clamp(L + k * COL_W, L + 2, R - 2), ns: [], t0: n.d.t, t1: n.d.t, top: base }));
      col.ns.push(n);
      col.t0 = Math.min(col.t0, n.d.t);
      col.t1 = Math.max(col.t1, n.d.t);
    }
    this.cols = [...byK.values()].sort((a, b) => a.x - b.x);
    for (const col of this.cols) col.ns.sort((a, b) => rank(a.d.role) - rank(b.d.role) || a.d.t - b.d.t);
    const maxStack = Math.max(1, ...this.cols.map((col) => col.ns.length));
    const unit = Math.min(8, (base - 2 - markTop) / maxStack);
    const gapPx = unit >= 4 ? 1.5 : unit >= 2 ? 0.75 : 0;

    // the stretch already played reads as lit
    c.fillStyle = "rgba(63,224,122,0.06)";
    c.fillRect(L, markTop - 2, playX - L, base - markTop + 2);
    // hovered column
    if (this.hoverCol) {
      c.fillStyle = "rgba(255,255,255,0.09)";
      c.fillRect(this.hoverCol.x - 5, markTop - 2, 10, base - markTop + 2);
    }
    // squeezed gaps: a dashed seam through the marks area
    c.strokeStyle = "rgba(228,179,106,0.35)";
    c.setLineDash([2, 3]);
    for (const b of g.timeBreaks) {
      const x = Math.round(ux(b.x)) + 0.5;
      c.beginPath();
      c.moveTo(x, markTop);
      c.lineTo(x, base);
      c.stroke();
    }
    c.setLineDash([]);

    const sel = g.selId;
    for (const col of this.cols) {
      let y = base - 1;
      for (const n of col.ns) {
        const shown = g.isShown(n);
        c.globalAlpha = shown ? 1 : 0.22;
        c.fillStyle = pigment(n.d.role);
        c.fillRect(col.x - 2.5, y - unit + gapPx, 5, Math.max(1, unit - gapPx));
        if (n.d.id === sel) {
          c.globalAlpha = 1;
          c.strokeStyle = "#ffffff";
          c.lineWidth = 1.5;
          c.strokeRect(col.x - 4.5, y - unit + gapPx - 2, 9, Math.max(1, unit - gapPx) + 4);
          c.lineWidth = 1;
        }
        y -= unit;
      }
      col.top = y;
    }
    c.globalAlpha = 1;

    // ---- axis, broken at squeezed gaps
    c.strokeStyle = "rgba(255,255,255,0.22)";
    c.beginPath();
    let ax = L;
    for (const b of g.timeBreaks) {
      const x = ux(b.x);
      c.moveTo(ax, base + 0.5);
      c.lineTo(x - 4, base + 0.5);
      ax = x + 4;
    }
    c.moveTo(ax, base + 0.5);
    c.lineTo(R, base + 0.5);
    c.stroke();
    c.strokeStyle = "rgba(228,179,106,0.8)";
    for (const b of g.timeBreaks) {
      const x = ux(b.x);
      c.beginPath();
      c.moveTo(x - 5, base + 3.5);
      c.lineTo(x - 2, base - 2.5);
      c.moveTo(x + 2, base + 3.5);
      c.lineTo(x + 5, base - 2.5);
      c.stroke();
    }

    // ---- labels under the axis: both ends, the gaps, then calendar ticks wherever they fit
    c.font = `500 10.5px ${MONO}`;
    c.textBaseline = "top";
    const taken: [number, number][] = [];
    const free = (a: number, b: number) => a >= L - 8 && b <= R + 8 && !taken.some(([p, q]) => a < q + 10 && b > p - 10);
    const put = (text: string, x: number, align: "left" | "right" | "center", color: string) => {
      const tw = c.measureText(text).width;
      const a = align === "left" ? x : align === "right" ? x - tw : x - tw / 2;
      if (!free(a, a + tw)) return false;
      taken.push([a, a + tw]);
      c.fillStyle = color;
      c.textAlign = align;
      c.fillText(text, x, base + 5);
      return true;
    };
    if (first != null && last != null) {
      const short = last - first < 2 * DAY;
      const end = (t: number) => (short ? fmtClock(t).slice(0, 5) : fmtShort(t));
      put(end(last), R, "right", "#c9c5bd");
      put(end(first), L, "left", "#c9c5bd");
      for (const b of g.timeBreaks) put(`${fmtDur(b.gap)} gap`, ux(b.x), "center", "#e4b36a");
      const key = `${first}|${last}`;
      if (this.tickCache.key !== key) this.tickCache = { key, ticks: calendarTicks(first, last) };
      const ticks = this.tickCache.ticks.map((tk) => ({ ...tk, x: ux(g.xForTime(tk.t)) }));
      let lastMark = -1e9;
      c.fillStyle = "rgba(255,255,255,0.3)";
      for (const tk of ticks) {
        if (tk.x - lastMark < 3) continue;
        lastMark = tk.x;
        c.fillRect(Math.round(tk.x), base + 1, 1, tk.major ? 4 : 3);
      }
      for (const pass of [true, false]) for (const tk of ticks) if (tk.major === pass) put(tk.label, tk.x, "center", tk.major ? "#c9c5bd" : "#8a867e");
    }

    // ---- flags for the two posts the story hangs on
    c.font = `600 10px ${SANS}`;
    c.textBaseline = "alphabetic";
    const flagTaken: [number, number][] = [];
    const flag = (id: string, text: string, col: string) => {
      const n = g.byId.get(id);
      const at = n && this.cols.find((cl) => cl.ns.includes(n));
      if (!n || !at) return;
      const tw = c.measureText(text).width;
      const a = clamp(at.x - tw / 2, L - 6, R + 6 - tw);
      if (flagTaken.some(([p, q]) => a < q + 6 && a + tw > p - 6)) return;
      flagTaken.push([a, a + tw]);
      c.globalAlpha = g.isShown(n) ? 1 : 0.45;
      c.fillStyle = col;
      c.textAlign = "left";
      c.fillText(text, a, flagY);
      c.fillRect(at.x - 0.5, flagY + 2, 1, Math.max(0, at.top - flagY - 3));
      c.globalAlpha = 1;
    };
    flag(g.case.submittedId, "Submitted", ON_BLACK.submitted);
    if (g.case.originId !== g.case.submittedId) flag(g.case.originId, "Earliest", ON_BLACK.origin);

    // ---- hover ghost line
    if (this.hoverX != null && !this.hoverCol && !this.dragging) {
      c.fillStyle = "rgba(255,255,255,0.35)";
      c.fillRect(Math.round(this.hoverX), markTop - 2, 1, base - markTop + 2);
    }
    // ---- playhead: a line with a grab knob on the axis
    c.fillStyle = "#3fe07a";
    c.shadowColor = "#3fe07a";
    c.shadowBlur = 6;
    c.fillRect(playX - 0.75, markTop - 4, 1.5, base - markTop + 4);
    c.shadowBlur = 0;
    c.beginPath();
    c.arc(playX, base + 0.5, 4.5, 0, Math.PI * 2);
    c.fill();
    c.strokeStyle = "#08080a";
    c.lineWidth = 1.5;
    c.stroke();
    c.lineWidth = 1;

    // ---- readout
    if (first == null || last == null) return;
    const t = this.u >= 0.9999 || !Number.isFinite(g.now) ? last : g.now;
    const posts = this.posts();
    const shownN = posts.filter((n) => g.isShown(n)).length;
    const d = this.clock.querySelector(".d")!;
    const rel = this.clock.querySelector(".rel")!;
    const txt = `${fmtDate(t, false)} · ${fmtClock(t)}`;
    // only a real time sets "the first": a crawl date (undated) or a lookup start (time unknown) states no interval
    const dated = posts.filter((n) => !n.d.undated && !n.d.timeUnknown).map((n) => n.d.t);
    const firstCopy = dated.length ? Math.min(...dated) : NaN;
    const N = posts.length;
    const count = shownN >= N ? (N === 1 ? "1 post" : `All ${N} posts`) : `${shownN} of ${N} posts`;
    const since = Number.isNaN(firstCopy) ? " · time of the first unknown" : Math.abs(t - firstCopy) < 1000 ? (shownN ? " · the first post" : "") : t < firstCopy ? ` · ${fmtDur(firstCopy - t)} before the first` : ` · ${fmtDur(t - firstCopy)} after the first`;
    const r2 = count + since;
    if (d.textContent !== txt) d.textContent = txt;
    if (rel.textContent !== r2) {
      rel.textContent = r2;
      this.slider.setAttribute("aria-valuetext", `${txt}, ${r2}`);
    }
    const v = String(Math.round(this.u * 1000));
    if (this.slider.value !== v) this.slider.value = v;
  }
}

/** Date labels across the top of the stage, aligned with the time axis. */
export function drawRuler(canvas: HTMLCanvasElement, g: Graph) {
  const { ctx: c, w, h } = fitCanvas(canvas);
  c.clearRect(0, 0, w, h);
  if (g.mode !== "timeline" || !g.case) return;
  const ns = g.nodes.filter((n) => n.vis && !n.d.undated).sort((a, b) => a.tx - b.tx);
  c.font = `600 11px ${SANS}`;
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
    if (s.x + 5 + c.measureText(label).width > w - 6) continue; // never clip at the edge
    lastLabel = label;
    c.fillStyle = "rgba(255,255,255,0.6)";
    c.fillRect(s.x, h - 12, 1, 12);
    c.fillStyle = "#f2efe9";
    c.fillText(label, s.x + 5, h / 2 - 1);
    drawn.push([s.x, s.x + 5 + c.measureText(label).width]);
    lastX = s.x;
    lastT = n.d.t;
    firstDone = true;
  }
  c.textAlign = "center";
  let lastPill = -1e9;
  let prevBreak = -1e9;
  for (let bi = 0; bi < g.timeBreaks.length; bi++) {
    const b = g.timeBreaks[bi];
    const s = g.toScreen(b.x, 0);
    const from = prevBreak;
    prevBreak = s.x;
    const next = bi + 1 < g.timeBreaks.length ? g.toScreen(g.timeBreaks[bi + 1].x, 0).x : 1e9;
    // a gap pill only reads right between two visible dates
    if (!drawn.some(([a]) => a > from && a < s.x) || !drawn.some(([a]) => a > s.x && a < next)) continue;
    if (s.x < 0 || s.x > w || s.x - lastPill < 100) continue;
    const label = `${fmtDur(b.gap)} gap`;
    c.font = `600 10px ${SANS}`;
    const tw = c.measureText(label).width + 16;
    if (s.x - tw / 2 < 2 || s.x + tw / 2 > w - 2) continue;
    if (drawn.some(([a, b]) => s.x + tw / 2 + 6 > a && s.x - tw / 2 - 6 < b)) continue;
    lastPill = s.x;
    c.fillStyle = "#050506";
    c.fillRect(s.x - tw / 2, h / 2 - 10, tw, 18);
    c.fillStyle = "#f2efe9";
    c.fillText(label, s.x, h / 2);
    c.font = `600 11px ${SANS}`;
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
      c.fillStyle = roleOf(n.d.role).color;
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
