// Trackdown graph engine: DOM cards over canvas layers, custom force physics, inertial camera.
import type { Cluster, TraceCase, TraceEdge, TraceNode } from "../types";
import { cardHtml } from "../ui/card";
import { fmtDur, REL, ROLE, STATUS } from "../ui/format";

export type Mode = "timeline" | "web";

export interface VNode {
  d: TraceNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
  w: number;
  h: number;
  tx: number; // time-anchored x
  py: number; // preferred y (tidy tree)
  el: HTMLElement;
  vis: boolean;
  born: number;
  compact: boolean;
  parents: VNode[];
  children: VNode[];
  tilt: number;
  last: string;
  drag: boolean;
}
interface VEdge {
  e: TraceEdge;
  a: VNode;
  b: VNode;
  born: number;
  seed: number;
}
interface Ring {
  x: number;
  y: number;
  color: string;
  start: number;
  dur: number;
  r0: number;
  r1: number;
}
interface Ghost {
  x: number;
  y: number;
  w: number;
  h: number;
  start: number;
  dur: number;
  label: string;
}

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function bez(p0: number, p1: number, p2: number, p3: number, t: number) {
  const u = 1 - t;
  return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
}

export class Graph {
  stage: HTMLElement;
  world: HTMLElement;
  under: HTMLCanvasElement;
  over: HTMLCanvasElement;
  uctx: CanvasRenderingContext2D;
  octx: CanvasRenderingContext2D;
  W = 0;
  H = 0;
  dpr = 1;
  insets = { top: 44, bottom: 112, left: 24, right: 24 };

  case: TraceCase | null = null;
  nodes: VNode[] = [];
  byId = new Map<string, VNode>();
  edges: VEdge[] = [];
  mode: Mode = "timeline";
  alpha = 1;
  alphaTarget = 0;

  // camera
  cx = 0;
  cy = 0;
  k = 0.6;
  private camAnim: { x0: number; y0: number; k0: number; x1: number; y1: number; k1: number; t0: number; dur: number; bump: number } | null = null;
  private panV = { x: 0, y: 0 };
  private zoomTarget: { k: number; sx: number; sy: number } | null = null;

  // reveal state
  now = Infinity;
  revealed: Set<string> | null = null;

  // interaction
  hoverId: string | null = null;
  selId: string | null = null;
  hl: Set<string> | null = null;
  private pointers = new Map<number, { x: number; y: number }>();
  private dragNode: VNode | null = null;
  private down: { x: number; y: number; t: number; node: VNode | null; moved: boolean; wx: number; wy: number } | null = null;
  private pinch: { d: number; k: number; mx: number; my: number } | null = null;
  private lastMove = { x: 0, y: 0, t: 0 };

  rings: Ring[] = [];
  ghosts: Ghost[] = [];
  timeBreaks: { x: number; gap: number }[] = [];
  colors: Record<string, string> = {};
  onSelect: (id: string | null) => void = () => {};
  onHover: (id: string | null) => void = () => {};
  onFrame: () => void = () => {};
  cardOpacity = 1;
  private raf = 0;
  private lastT = performance.now();

  constructor(stage: HTMLElement) {
    this.stage = stage;
    this.under = this.mkCanvas(1);
    this.world = document.createElement("div");
    this.world.className = "world";
    this.world.style.zIndex = "2";
    stage.appendChild(this.world);
    this.over = this.mkCanvas(4);
    this.uctx = this.under.getContext("2d")!;
    this.octx = this.over.getContext("2d")!;
    for (const r of Object.keys(ROLE)) this.colors[r] = ROLE[r as keyof typeof ROLE].color;
    this.colors.shill = cssVar("--cinnabar");
    this.colors.chill = cssVar("--verdigris");
    this.colors.unsure = cssVar("--slate");
    this.colors.ink = cssVar("--ink");
    this.colors.ink2 = cssVar("--ink-2");
    this.colors.ink3 = cssVar("--ink-3");
    this.colors.paper = cssVar("--paper");
    new ResizeObserver(() => this.resize()).observe(stage);
    this.resize();
    this.bind();
    this.raf = requestAnimationFrame(this.frame);
  }

  private mkCanvas(z: number) {
    const c = document.createElement("canvas");
    c.className = "layer";
    c.style.zIndex = String(z);
    if (z > 2) c.style.pointerEvents = "none";
    this.stage.appendChild(c);
    return c;
  }

  resize() {
    const r = this.stage.getBoundingClientRect();
    this.W = r.width;
    this.H = r.height;
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    for (const c of [this.under, this.over]) {
      c.width = Math.round(this.W * this.dpr);
      c.height = Math.round(this.H * this.dpr);
    }
  }

  // ------------------------------------------------------------------ loading
  load(c: TraceCase, opts: { revealAll: boolean } = { revealAll: true }) {
    this.case = c;
    this.world.innerHTML = "";
    this.nodes = [];
    this.byId.clear();
    this.edges = [];
    this.rings = [];
    this.ghosts = [];
    this.selId = null;
    this.hoverId = null;
    this.hl = null;
    const childCount = new Map<string, number>();
    for (const e of c.edges) childCount.set(e.from, (childCount.get(e.from) ?? 0) + 1);
    const order = new Map([...c.nodes].sort((a, b) => a.t - b.t).map((n, i) => [n.id, i + 1]));
    for (const d of c.nodes) {
      const el = document.createElement("div");
      const compact = d.role === "amplifier" && !childCount.get(d.id);
      el.className = `card role-${d.role} plat-${d.platform}${compact ? " chip" : ""}`;
      el.dataset.id = d.id;
      el.innerHTML = cardHtml(d, compact, order.get(d.id) ?? 0);
      this.world.appendChild(el);
      const n: VNode = { d, x: 0, y: 0, vx: 0, vy: 0, w: 0, h: 0, tx: 0, py: 0, el, vis: false, born: 0, compact, parents: [], children: [], tilt: 0, last: "", drag: false };
      this.nodes.push(n);
      this.byId.set(d.id, n);
    }
    // measure (world is unscaled layout space)
    for (const n of this.nodes) {
      n.w = n.el.offsetWidth;
      n.h = n.el.offsetHeight;
    }
    for (const e of c.edges) {
      const a = this.byId.get(e.from);
      const b = this.byId.get(e.to);
      if (!a || !b) continue;
      a.children.push(b);
      b.parents.push(a);
      this.edges.push({ e, a, b, born: 0, seed: Math.random() });
    }
    this.layoutTime();
    this.layoutTree();
    for (const n of this.nodes) {
      n.x = n.tx;
      n.y = n.py;
      n.el.style.display = "none";
    }
    this.revealed = opts.revealAll ? null : new Set();
    this.now = Infinity;
    this.syncVisibility(true);
    this.alpha = 1;
    // settle a bit offscreen so the first frame is calm
    if (opts.revealAll) for (let i = 0; i < 160; i++) this.step(1, true);
  }

  /** x = compressed time: each gap grows with log(gap), so seconds and years fit on one axis.
   *  Bursts (posts < 2 min apart) become a time-ordered grid: columns left to right, so a
   *  31-account burst reads as one dense block instead of a 2000 px column. */
  private layoutTime() {
    const sorted = [...this.nodes].sort((a, b) => a.d.t - b.d.t);
    const groups: VNode[][] = [];
    for (const n of sorted) {
      const g = groups[groups.length - 1];
      if (g && n.d.t - g[g.length - 1].d.t < 120000) g.push(n);
      else groups.push([n]);
    }
    let x = 0;
    let prevT = sorted[0]?.d.t ?? 0;
    this.timeBreaks = [];
    const span = (sorted[sorted.length - 1]?.d.t ?? 0) - prevT;
    const breakAt = Math.max(3 * 86400000, span / 40);
    this.grid = new Map();
    const xs: number[] = [];
    const COLW = 236;
    for (const g of groups) {
      const gap = g[0].d.t - prevT;
      const dx = gap <= 0 ? 0 : Math.min(40 * Math.log2(1 + gap / 60000), 280 + 18 * Math.log2(1 + gap / 86400000));
      if (gap > breakAt) this.timeBreaks.push({ x: x + dx / 2, gap });
      x += dx;
      const chips = g.filter((n) => n.compact).length;
      if (g.length > 6 && chips >= g.length / 2) {
        const cols = Math.ceil(g.length / 8);
        const rows = Math.ceil(g.length / cols);
        const id = g[0].d.id;
        g.forEach((n, i) => {
          const col = Math.floor(i / rows);
          n.tx = x + col * COLW;
          this.grid.set(n, { id, row: i % rows, rows });
          xs.push(n.tx);
        });
        x += (cols - 1) * COLW;
      } else {
        // small groups: spread a little so they don't sit on one x
        g.forEach((n, i) => {
          n.tx = x + i * 14;
          xs.push(n.tx);
        });
        x += (g.length - 1) * 14;
      }
      prevT = g[g.length - 1].d.t;
    }
    // centre the axis on 0
    const mid = (Math.min(...xs) + Math.max(...xs)) / 2;
    for (const n of this.nodes) n.tx -= mid;
    for (const b of this.timeBreaks) b.x -= mid;
    this.timeMap = sorted.map((n) => [n.d.t, n.tx] as [number, number]);
    // keep the map monotonic for scrubbing
    for (let i = 1; i < this.timeMap.length; i++) if (this.timeMap[i][1] < this.timeMap[i - 1][1]) this.timeMap[i][1] = this.timeMap[i - 1][1];
  }
  grid = new Map<VNode, { id: string; row: number; rows: number }>();
  timeMap: [number, number][] = [];

  /** world x for an arbitrary time (piecewise linear between known nodes) */
  xForTime(t: number): number {
    const m = this.timeMap;
    if (!m.length) return 0;
    if (t <= m[0][0]) return m[0][1];
    for (let i = 1; i < m.length; i++) {
      if (t <= m[i][0]) {
        const [t0, x0] = m[i - 1];
        const [t1, x1] = m[i];
        return t1 === t0 ? x1 : lerp(x0, x1, (t - t0) / (t1 - t0));
      }
    }
    return m[m.length - 1][1];
  }

  /** Tidy-tree y: leaves get consecutive slots, parents sit at their children's mean. */
  private layoutTree() {
    const roots = this.nodes.filter((n) => !n.parents.length);
    let slot = 0;
    const seen = new Set<VNode>();
    const gridTop = new Map<string, number>();
    const visit = (n: VNode): number => {
      if (seen.has(n)) return n.py;
      seen.add(n);
      const kids = n.children.filter((c) => c.parents[0] === n).sort((a, b) => a.d.t - b.d.t);
      if (!kids.length) {
        const gr = this.grid.get(n);
        if (gr) {
          const RH = 64;
          let top = gridTop.get(gr.id);
          if (top == null) {
            top = slot;
            gridTop.set(gr.id, top);
            slot += gr.rows * RH + 30;
          }
          n.py = top + gr.row * RH + RH / 2;
          return n.py;
        }
        const gap = n.compact ? 62 : n.h + 34;
        n.py = slot + gap / 2;
        slot += gap;
        return n.py;
      }
      const ys = kids.map(visit);
      n.py = (Math.min(...ys) + Math.max(...ys)) / 2;
      return n.py;
    };
    roots.sort((a, b) => a.d.t - b.d.t).forEach(visit);
    const mid = slot / 2;
    for (const n of this.nodes) n.py -= mid;
  }

  setMode(m: Mode) {
    if (m === this.mode) return;
    this.mode = m;
    this.reheat(1);
  }

  reheat(a = 0.6) {
    this.alpha = Math.max(this.alpha, a);
  }

  // ------------------------------------------------------------------ visibility
  isShown(n: VNode) {
    return this.revealed ? this.revealed.has(n.d.id) : n.d.t <= this.now;
  }

  setNow(t: number) {
    this.now = t;
    this.syncVisibility(false);
  }

  revealAll() {
    this.revealed = null;
    this.now = Infinity;
    this.syncVisibility(false);
  }

  reveal(ids: string[], from?: string) {
    if (!this.revealed) this.revealed = new Set();
    for (const id of ids) this.revealed.add(id);
    this.syncVisibility(false, from);
  }

  private syncVisibility(instant: boolean, from?: string) {
    const now = performance.now();
    let changed = false;
    for (const n of this.nodes) {
      const want = this.isShown(n);
      if (want === n.vis) continue;
      changed = true;
      n.vis = want;
      if (want) {
        n.born = now;
        n.el.style.display = "";
        if (!instant) {
          const src = (from && this.byId.get(from)) || n.parents.find((p) => p.vis) || n.children.find((c) => c.vis);
          if (src) {
            n.x = src.x + (Math.random() - 0.5) * 30;
            n.y = src.y + (Math.random() - 0.5) * 30;
            n.vx = (n.tx - n.x) * 0.06;
            n.vy = (n.py - n.y) * 0.06;
          } else {
            n.x = this.mode === "timeline" ? n.tx : this.cx + (Math.random() - 0.5) * 200;
            n.y = n.py;
          }
          n.el.classList.remove("enter");
          void n.el.offsetWidth;
          n.el.classList.add("enter");
          window.setTimeout(() => n.el.classList.remove("enter"), 800);
        }
      } else {
        n.el.style.display = "none";
        if (this.selId === n.d.id) this.select(null);
      }
    }
    for (const e of this.edges) {
      const both = e.a.vis && e.b.vis;
      if (both && !e.born) e.born = instant ? now - 5000 : now;
      if (!both) e.born = 0;
    }
    if (changed) this.reheat(instant ? 0.3 : 0.7);
  }

  // ------------------------------------------------------------------ physics
  step(dt: number, silent = false) {
    const ns = this.nodes.filter((n) => n.vis);
    if (!ns.length) return;
    const a = this.alpha;
    const tl = this.mode === "timeline";
    // pairwise repulsion
    for (let i = 0; i < ns.length; i++) {
      const p = ns[i];
      for (let j = i + 1; j < ns.length; j++) {
        const q = ns[j];
        let dx = q.x - p.x;
        let dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 700 * 700) continue;
        if (d2 < 1) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          d2 = 1;
        }
        const f = ((tl ? 2600 : 9000) * a) / Math.max(d2, 900);
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        p.vx -= fx * (tl ? 0.15 : 1);
        p.vy -= fy;
        q.vx += fx * (tl ? 0.15 : 1);
        q.vy += fy;
      }
    }
    // links
    for (const e of this.edges) {
      if (!e.a.vis || !e.b.vis) continue;
      const { a: p, b: q } = e;
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      if (tl) {
        const k = 0.012 * a * (q.compact ? 0.5 : 1);
        p.vy += dy * k * 0.4;
        q.vy -= dy * k;
      } else {
        const d = Math.hypot(dx, dy) || 1;
        const rest = (p.w + q.w) / 2 + (q.compact ? 40 : 90);
        const f = (d - rest) * 0.02 * a;
        p.vx += (dx / d) * f * 0.5;
        p.vy += (dy / d) * f * 0.5;
        q.vx -= (dx / d) * f;
        q.vy -= (dy / d) * f;
      }
    }
    for (const n of ns) {
      if (tl) {
        n.vx += (n.tx - n.x) * 0.05 * Math.max(a, 0.25);
        n.vy += (n.py - n.y) * 0.004 * a;
      } else {
        n.vx += -n.x * 0.0016 * a;
        n.vy += -n.y * 0.0022 * a;
      }
      if (n.drag) continue;
      n.vx *= 0.8;
      n.vy *= 0.8;
      n.x += n.vx * dt;
      n.y += n.vy * dt;
    }
    // rectangle collisions (positional)
    const pad = 16;
    for (let it = 0; it < 3; it++) {
      for (let i = 0; i < ns.length; i++) {
        const p = ns[i];
        for (let j = i + 1; j < ns.length; j++) {
          const q = ns[j];
          const ox = (p.w + q.w) / 2 + pad - Math.abs(q.x - p.x);
          if (ox <= 0) continue;
          const oy = (p.h + q.h) / 2 + pad - Math.abs(q.y - p.y);
          if (oy <= 0) continue;
          const wp = p.drag ? 0 : q.drag ? 1 : 0.5;
          const wq = 1 - wp;
          if (oy < ox * (tl ? 1.6 : 1)) {
            const s = (q.y >= p.y ? 1 : -1) * oy * 0.5;
            p.y -= s * wp * 2 * 0.5;
            q.y += s * wq * 2 * 0.5;
          } else {
            const s = (q.x >= p.x ? 1 : -1) * ox * 0.5;
            p.x -= s * wp * 2 * 0.5;
            q.x += s * wq * 2 * 0.5;
          }
        }
      }
    }
    this.alpha += (this.alphaTarget - this.alpha) * (silent ? 0.03 : 0.012);
  }

  // ------------------------------------------------------------------ camera
  toWorld(sx: number, sy: number) {
    return { x: (sx - this.W / 2) / this.k + this.cx, y: (sy - this.H / 2) / this.k + this.cy };
  }
  toScreen(x: number, y: number) {
    return { x: (x - this.cx) * this.k + this.W / 2, y: (y - this.cy) * this.k + this.H / 2 };
  }

  flyTo(x: number, y: number, k: number, dur = 900) {
    const dist = Math.hypot(x - this.cx, y - this.cy) * Math.min(this.k, k);
    const bump = clamp(dist / Math.max(this.W, 1) - 0.4, 0, 1.2) * 0.6;
    this.camAnim = { x0: this.cx, y0: this.cy, k0: this.k, x1: x, y1: y, k1: k, t0: performance.now(), dur, bump };
    this.panV = { x: 0, y: 0 };
    this.zoomTarget = null;
  }

  /** Camera that fits the given nodes inside the visible stage area (minus HUD insets). */
  fit(ids?: string[], dur = 900, maxK = 1.1) {
    const ns = (ids ? ids.map((i) => this.byId.get(i)!).filter(Boolean) : this.nodes).filter((n) => n.vis);
    if (!ns.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) {
      x0 = Math.min(x0, n.x - n.w / 2);
      x1 = Math.max(x1, n.x + n.w / 2);
      y0 = Math.min(y0, n.y - n.h / 2);
      y1 = Math.max(y1, n.y + n.h / 2);
    }
    const { top, bottom, left, right } = this.insets;
    const aw = this.W - left - right - 40;
    const ah = this.H - top - bottom - 40;
    const k = clamp(Math.min(aw / (x1 - x0), ah / (y1 - y0)), 0.08, maxK);
    // shift centre so the box sits in the inset area
    const offX = (left - right) / 2 / k;
    const offY = (top - bottom) / 2 / k;
    this.flyTo((x0 + x1) / 2 - offX, (y0 + y1) / 2 - offY, k, dur);
  }

  /** keep re-framing everything while the layout settles; any user input stops it */
  private followUntil = 0;
  private followNext = 0;
  followFit(ms = 2500) {
    this.followUntil = performance.now() + ms;
    this.followNext = 0;
  }
  stopFollow() {
    this.followUntil = 0;
  }

  focus(id: string, k = 1.05, dur = 800) {
    const n = this.byId.get(id);
    if (!n) return;
    const offY = (this.insets.top - this.insets.bottom) / 2 / k;
    this.flyTo(n.x, n.y - offY, k, dur);
  }

  zoomBy(f: number, sx = this.W / 2, sy = this.H / 2) {
    this.stopFollow();
    const base = this.zoomTarget?.k ?? this.k;
    this.camAnim = null;
    this.zoomTarget = { k: clamp(base * f, 0.06, 2.6), sx, sy };
  }

  private cameraStep(dt: number) {
    const now = performance.now();
    if (this.camAnim) {
      const c = this.camAnim;
      const t = clamp((now - c.t0) / c.dur, 0, 1);
      const e = easeInOut(t);
      this.cx = lerp(c.x0, c.x1, e);
      this.cy = lerp(c.y0, c.y1, e);
      this.k = Math.exp(lerp(Math.log(c.k0), Math.log(c.k1), e) - c.bump * Math.sin(Math.PI * e));
      if (t >= 1) this.camAnim = null;
      return;
    }
    if (this.zoomTarget) {
      const z = this.zoomTarget;
      const before = this.toWorld(z.sx, z.sy);
      this.k = Math.exp(lerp(Math.log(this.k), Math.log(z.k), 1 - Math.pow(0.78, dt)));
      const after = this.toWorld(z.sx, z.sy);
      this.cx += before.x - after.x;
      this.cy += before.y - after.y;
      if (Math.abs(Math.log(this.k / z.k)) < 0.002) this.zoomTarget = null;
    }
    if (!this.pointers.size && (Math.abs(this.panV.x) > 0.01 || Math.abs(this.panV.y) > 0.01)) {
      this.cx -= (this.panV.x * dt) / this.k;
      this.cy -= (this.panV.y * dt) / this.k;
      const f = Math.pow(0.92, dt);
      this.panV.x *= f;
      this.panV.y *= f;
    }
  }

  // ------------------------------------------------------------------ interaction
  hitTest(sx: number, sy: number): VNode | null {
    const p = this.toWorld(sx, sy);
    const far = this.cardOpacity < 0.5;
    let best: VNode | null = null;
    let bestD = Infinity;
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      if (!n.vis) continue;
      if (far) {
        const d = Math.hypot(p.x - n.x, p.y - n.y) * this.k;
        if (d < 16 && d < bestD) {
          best = n;
          bestD = d;
        }
      } else if (Math.abs(p.x - n.x) <= n.w / 2 && Math.abs(p.y - n.y) <= n.h / 2) return n;
    }
    return best;
  }

  private bind() {
    const st = this.stage;
    const local = (e: PointerEvent | WheelEvent | MouseEvent) => {
      const r = st.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    st.addEventListener("pointerdown", (e) => {
      if ((e.target as HTMLElement).closest("[data-ui]")) return;
      const p = local(e);
      this.pointers.set(e.pointerId, p);
      st.setPointerCapture(e.pointerId);
      this.stopFollow();
      this.camAnim = null;
      this.zoomTarget = null;
      if (this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        this.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: this.k, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
        this.releaseDrag();
        this.down = null;
        return;
      }
      const node = this.hitTest(p.x, p.y);
      const w = this.toWorld(p.x, p.y);
      this.down = { x: p.x, y: p.y, t: performance.now(), node, moved: false, wx: node ? w.x - node.x : 0, wy: node ? w.y - node.y : 0 };
      this.panV = { x: 0, y: 0 };
      this.lastMove = { x: p.x, y: p.y, t: performance.now() };
    });
    st.addEventListener("pointermove", (e) => {
      const p = local(e);
      if (!this.pointers.has(e.pointerId)) {
        const n = this.hitTest(p.x, p.y);
        const id = n?.d.id ?? null;
        if (id !== this.hoverId) {
          this.hoverId = id;
          this.onHover(id);
        }
        return;
      }
      const prev = this.pointers.get(e.pointerId)!;
      this.pointers.set(e.pointerId, p);
      if (this.pinch && this.pointers.size === 2) {
        const [a, b] = [...this.pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        const before = this.toWorld(this.pinch.mx, this.pinch.my);
        this.k = clamp((this.pinch.k * d) / this.pinch.d, 0.06, 2.6);
        const after = this.toWorld(mx, my);
        this.cx += before.x - after.x;
        this.cy += before.y - after.y;
        this.pinch.mx = mx;
        this.pinch.my = my;
        return;
      }
      const dn = this.down;
      if (!dn) return;
      if (!dn.moved && Math.hypot(p.x - dn.x, p.y - dn.y) > 4) {
        dn.moved = true;
        if (dn.node) {
          this.dragNode = dn.node;
          dn.node.drag = true;
          dn.node.el.classList.add("dragging");
          this.alphaTarget = 0.35;
          this.reheat(0.5);
        } else st.classList.add("panning");
      }
      if (!dn.moved) return;
      const now = performance.now();
      if (this.dragNode) {
        const w = this.toWorld(p.x, p.y);
        const n = this.dragNode;
        const nx = w.x - dn.wx;
        const ny = w.y - dn.wy;
        n.vx = lerp(n.vx, nx - n.x, 0.6);
        n.vy = lerp(n.vy, ny - n.y, 0.6);
        n.x = nx;
        n.y = ny;
      } else {
        const dx = p.x - prev.x;
        const dy = p.y - prev.y;
        this.cx -= dx / this.k;
        this.cy -= dy / this.k;
        const dtm = Math.max(1, now - this.lastMove.t) / 16.67;
        this.panV = { x: lerp(this.panV.x, dx / dtm, 0.5), y: lerp(this.panV.y, dy / dtm, 0.5) };
      }
      this.lastMove = { x: p.x, y: p.y, t: now };
    });
    const up = (e: PointerEvent) => {
      if (!this.pointers.has(e.pointerId)) return;
      this.pointers.delete(e.pointerId);
      if (this.pointers.size < 2) this.pinch = null;
      const dn = this.down;
      st.classList.remove("panning");
      if (performance.now() - this.lastMove.t > 80) this.panV = { x: 0, y: 0 };
      if (dn && !dn.moved && e.type === "pointerup") {
        this.select(dn.node ? dn.node.d.id : null);
      }
      this.releaseDrag();
      this.down = null;
    };
    st.addEventListener("pointerup", up);
    st.addEventListener("pointercancel", up);
    st.addEventListener("dblclick", (e) => {
      if ((e.target as HTMLElement).closest("[data-ui]")) return;
      const p = local(e);
      const n = this.hitTest(p.x, p.y);
      if (n) this.focus(n.d.id, Math.max(1.1, this.k * 1.4));
      else this.fit();
    });
    st.addEventListener(
      "wheel",
      (e) => {
        if ((e.target as HTMLElement).closest("[data-ui]")) return;
        e.preventDefault();
        this.stopFollow();
        const p = local(e);
        if (e.shiftKey && !e.ctrlKey) {
          this.cx += (e.deltaY || e.deltaX) / this.k;
          return;
        }
        const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
        this.zoomBy(Math.exp(-dy * (e.ctrlKey ? 0.012 : 0.0016)), p.x, p.y);
      },
      { passive: false },
    );
    st.addEventListener("pointerleave", () => {
      if (this.hoverId) {
        this.hoverId = null;
        this.onHover(null);
      }
    });
  }

  private releaseDrag() {
    const n = this.dragNode;
    if (!n) return;
    n.drag = false;
    n.el.classList.remove("dragging");
    // keep the fling, capped
    n.vx = clamp(n.vx, -60, 60);
    n.vy = clamp(n.vy, -60, 60);
    this.dragNode = null;
    this.alphaTarget = 0;
    this.reheat(0.6);
  }

  select(id: string | null) {
    if (this.selId) this.byId.get(this.selId)?.el.classList.remove("sel");
    this.selId = id;
    if (id) this.byId.get(id)?.el.classList.add("sel");
    this.onSelect(id);
  }

  /** ancestors + descendants of a node */
  lineage(id: string): Set<string> {
    const out = new Set<string>([id]);
    const n = this.byId.get(id);
    if (!n) return out;
    const up = [n];
    while (up.length) {
      const c = up.pop()!;
      for (const p of c.parents) if (!out.has(p.d.id)) (out.add(p.d.id), up.push(p));
    }
    const down = [n];
    while (down.length) {
      const c = down.pop()!;
      for (const k of c.children) if (!out.has(k.d.id)) (out.add(k.d.id), down.push(k));
    }
    return out;
  }

  /** path from node up to the root via first parents */
  pathToOrigin(id: string): string[] {
    const out: string[] = [];
    let n = this.byId.get(id);
    const seen = new Set<string>();
    while (n && !seen.has(n.d.id)) {
      seen.add(n.d.id);
      out.push(n.d.id);
      n = n.parents[0];
    }
    return out;
  }

  setHighlight(ids: Set<string> | null) {
    this.hl = ids;
    for (const n of this.nodes) n.el.classList.toggle("dim", !!ids && !ids.has(n.d.id));
  }

  ring(id: string, color: string, r1 = 340, dur = 1300) {
    const n = this.byId.get(id);
    if (!n) return;
    this.rings.push({ x: n.x, y: n.y, color, start: performance.now(), dur, r0: Math.max(n.w, n.h) / 2, r1 });
  }

  ghost(nearId: string, count: number, label = "no match") {
    const n = this.byId.get(nearId);
    if (!n) return;
    const now = performance.now();
    for (let i = 0; i < count; i++) {
      const ang = Math.PI * (0.6 + Math.random() * 0.8) + (i % 2 ? 0 : Math.PI);
      const dist = 240 + Math.random() * 220;
      this.ghosts.push({ x: n.x + Math.cos(ang) * dist - 180, y: n.y + Math.sin(ang) * dist * 0.8, w: 200 + Math.random() * 60, h: 70 + Math.random() * 50, start: now + i * 120, dur: 1500, label });
    }
  }

  // ------------------------------------------------------------------ render
  private frame = (t: number) => {
    const dt = clamp((t - this.lastT) / 16.67, 0.2, 2.5);
    this.lastT = t;
    if (this.alpha > 0.004 || this.dragNode) this.step(dt);
    if (t < this.followUntil && t > this.followNext) {
      this.followNext = t + 450;
      this.fit(undefined, 700);
    }
    this.cameraStep(dt);
    this.render(t);
    this.onFrame();
    this.raf = requestAnimationFrame(this.frame);
  };

  destroy() {
    cancelAnimationFrame(this.raf);
  }

  private render(now: number) {
    const k = this.k;
    const ox = this.W / 2 - this.cx * k;
    const oy = this.H / 2 - this.cy * k;
    // semantic zoom: cards fade out under ~0.42, dots fade in
    const op = clamp((k - 0.15) / 0.08, 0, 1);
    if (Math.abs(op - this.cardOpacity) > 0.001) {
      this.cardOpacity = op;
      this.world.style.setProperty("--card-op", op.toFixed(3));
      this.world.style.visibility = op < 0.02 ? "hidden" : "";
    }
    this.world.style.transform = `translate(${ox}px, ${oy}px) scale(${k})`;
    for (const n of this.nodes) {
      if (!n.vis) continue;
      const target = n.drag ? clamp(n.vx * 0.5, -7, 7) : 0;
      n.tilt += (target - n.tilt) * 0.18;
      const tf = `translate(${(n.x - n.w / 2).toFixed(1)}px, ${(n.y - n.h / 2).toFixed(1)}px)${Math.abs(n.tilt) > 0.05 ? ` rotate(${n.tilt.toFixed(2)}deg)` : ""}${n.drag ? " scale(1.03)" : ""}`;
      if (tf !== n.last) {
        n.el.style.transform = tf;
        n.last = tf;
      }
    }
    this.drawUnder(now, ox, oy, op);
    this.drawOver(now, ox, oy);
  }

  private drawUnder(now: number, ox: number, oy: number, op: number) {
    const c = this.uctx;
    const k = this.k;
    const dpr = this.dpr;
    const C = this.colors;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, this.W, this.H);
    // ruled codex grid (world space), finer lines fade out when zoomed out
    let step = 64;
    while (step * k < 26) step *= 2;
    const gx0 = Math.floor((0 - ox) / k / step) * step;
    const gy0 = Math.floor((0 - oy) / k / step) * step;
    c.lineWidth = 1;
    for (let x = gx0, i = Math.round(gx0 / step); x * k + ox < this.W; x += step, i++) {
      const sx = Math.round(x * k + ox) + 0.5;
      c.strokeStyle = i % 4 === 0 ? "rgba(90,70,40,0.11)" : "rgba(90,70,40,0.05)";
      c.beginPath();
      c.moveTo(sx, 0);
      c.lineTo(sx, this.H);
      c.stroke();
    }
    for (let y = gy0, i = Math.round(gy0 / step); y * k + oy < this.H; y += step, i++) {
      const sy = Math.round(y * k + oy) + 0.5;
      c.strokeStyle = i % 4 === 0 ? "rgba(90,70,40,0.11)" : "rgba(90,70,40,0.05)";
      c.beginPath();
      c.moveTo(0, sy);
      c.lineTo(this.W, sy);
      c.stroke();
    }
    c.setTransform(dpr * k, 0, 0, dpr * k, dpr * ox, dpr * oy);
    // compressed time = a lacuna: hatched band
    if (this.mode === "timeline") {
      const top = (0 - oy) / k;
      const bot = (this.H - oy) / k;
      for (const b of this.timeBreaks) {
        const w = 56;
        c.save();
        c.beginPath();
        c.rect(b.x - w / 2, top, w, bot - top);
        c.clip();
        c.strokeStyle = "rgba(90,70,40,0.14)";
        c.lineWidth = 1 / k;
        const gap = 9;
        for (let y = top - w; y < bot; y += gap) {
          c.beginPath();
          c.moveTo(b.x - w / 2, y + w);
          c.lineTo(b.x + w / 2, y);
          c.stroke();
        }
        c.restore();
        c.strokeStyle = "rgba(90,70,40,0.3)";
        c.lineWidth = 1 / k;
        c.beginPath();
        c.moveTo(b.x - w / 2, top);
        c.lineTo(b.x - w / 2, bot);
        c.moveTo(b.x + w / 2, top);
        c.lineTo(b.x + w / 2, bot);
        c.stroke();
      }
    }
    // cluster engravings
    if (this.case) this.case.clusters.forEach((cl, i) => this.drawHull(c, cl, now, i));
    // edges: ink lines
    const hl = this.hl;
    c.lineCap = "round";
    for (const e of this.edges) {
      if (!e.a.vis || !e.b.vis) continue;
      const pts = this.edgePts(e);
      const prog = clamp((now - e.born) / 700, 0, 1);
      const on = hl ? hl.has(e.a.d.id) && hl.has(e.b.d.id) : false;
      const dim = hl && !on;
      const role = this.colors[e.b.d.role] ?? C.ink2;
      const col = on ? (e.b.d.role === "submitted" || e.b.d.role === "relay" ? C.ink : role) : C.ink2;
      c.save();
      c.globalAlpha = dim ? 0.12 : on ? 1 : 0.55;
      c.strokeStyle = col;
      c.lineWidth = (on ? 2.2 : 1.1) / k;
      c.setLineDash(REL[e.e.rel].dash.map((d) => d / Math.sqrt(k)));
      c.beginPath();
      c.moveTo(pts[0], pts[1]);
      if (prog >= 1) c.bezierCurveTo(pts[2], pts[3], pts[4], pts[5], pts[6], pts[7]);
      else {
        const N = 28;
        for (let i = 1; i <= N * prog; i++) {
          const t = i / N;
          c.lineTo(bez(pts[0], pts[2], pts[4], pts[6], t), bez(pts[1], pts[3], pts[5], pts[7], t));
        }
      }
      c.stroke();
      c.restore();
      if (prog < 1) {
        // the nib drawing the line
        const hx = bez(pts[0], pts[2], pts[4], pts[6], prog);
        const hy = bez(pts[1], pts[3], pts[5], pts[7], prog);
        c.fillStyle = C.ink;
        c.beginPath();
        c.arc(hx, hy, 3.2 / k, 0, Math.PI * 2);
        c.fill();
      }
      if (prog >= 1 && !dim) {
        // arrowhead into the child
        const tx = pts[6] - bez(pts[0], pts[2], pts[4], pts[6], 0.97);
        const ty = pts[7] - bez(pts[1], pts[3], pts[5], pts[7], 0.97);
        const L = Math.hypot(tx, ty) || 1;
        const ux = tx / L;
        const uy = ty / L;
        const s = (on ? 7 : 5.5) / k;
        c.fillStyle = col;
        c.globalAlpha = on ? 1 : 0.6;
        c.beginPath();
        c.moveTo(pts[6], pts[7]);
        c.lineTo(pts[6] - ux * s - uy * s * 0.45, pts[7] - uy * s + ux * s * 0.45);
        c.lineTo(pts[6] - ux * s + uy * s * 0.45, pts[7] - uy * s - ux * s * 0.45);
        c.closePath();
        c.fill();
        c.globalAlpha = 1;
        // ink travelling down the line, parent -> child
        const n = on ? 3 : 1;
        const speed = on ? 0.0005 : 0.00018;
        c.fillStyle = col;
        for (let i = 0; i < n; i++) {
          const u = (now * speed + e.seed + i / n) % 1;
          c.globalAlpha = (on ? 0.9 : 0.45) * Math.sin(u * Math.PI);
          c.beginPath();
          c.arc(bez(pts[0], pts[2], pts[4], pts[6], u), bez(pts[1], pts[3], pts[5], pts[7], u), (on ? 2.6 : 1.8) / k, 0, Math.PI * 2);
          c.fill();
        }
        c.globalAlpha = 1;
      }
      if (prog >= 1 && k > 0.5 && op > 0.5 && (e.e.sim != null || e.e.rel === "cites") && (!e.b.compact || on) && !dim) {
        const mx = bez(pts[0], pts[2], pts[4], pts[6], 0.5);
        const my = bez(pts[1], pts[3], pts[5], pts[7], 0.5);
        this.pill(c, mx, my, `${REL[e.e.rel].label}${e.e.sim != null ? `, ${Math.round(e.e.sim * 100)}%` : ""}`, on ? col : C.ink2);
      }
    }
    // far zoom: specimens as inked dots
    if (op < 1) {
      for (const n of this.nodes) {
        if (!n.vis) continue;
        const col = this.colors[n.d.role] ?? C.ink2;
        const big = n.d.role === "origin" || n.d.role === "submitted";
        const rad = (big ? 8 : n.compact ? 4 : 6) / k;
        const dim = hl && !hl.has(n.d.id);
        c.globalAlpha = clamp(1 - op * 1.7, 0, 1) * (dim ? 0.25 : 1);
        c.fillStyle = col;
        c.strokeStyle = C.ink;
        c.lineWidth = 1 / k;
        c.beginPath();
        c.arc(n.x, n.y, rad, 0, Math.PI * 2);
        c.fill();
        c.stroke();
        if (n.d.role === "origin") {
          c.beginPath();
          c.arc(n.x, n.y, rad + 4 / k, 0, Math.PI * 2);
          c.strokeStyle = col;
          c.stroke();
        }
        if (big || n.d.role === "mutation" || n.d.id === this.hoverId || n.d.id === this.selId) {
          c.font = `italic 400 ${15 / k}px "EB Garamond", serif`;
          c.fillStyle = C.ink;
          c.textAlign = "center";
          c.fillText(n.d.role === "origin" ? "the original" : n.d.role === "submitted" ? "submitted" : n.d.author.name, n.x, n.y - rad - 8 / k);
        }
      }
      c.globalAlpha = 1;
    }
    // rejected candidates: pencil outlines, struck through
    for (const g of this.ghosts) {
      const t = (now - g.start) / g.dur;
      if (t < 0 || t > 1) continue;
      const a = t < 0.15 ? t / 0.15 : 1 - (t - 0.15) / 0.85;
      c.save();
      c.globalAlpha = a * 0.75;
      c.strokeStyle = C.ink3;
      c.setLineDash([4 / k, 3 / k]);
      c.lineWidth = 1 / k;
      c.strokeRect(g.x - g.w / 2, g.y - g.h / 2, g.w, g.h);
      c.setLineDash([]);
      c.fillStyle = "rgba(251,248,240,0.5)";
      c.fillRect(g.x - g.w / 2, g.y - g.h / 2, g.w, g.h);
      // a few pencil text lines
      c.strokeStyle = "rgba(125,111,93,0.5)";
      for (let i = 0; i < 3; i++) {
        c.beginPath();
        c.moveTo(g.x - g.w / 2 + 12, g.y - g.h / 2 + 18 + i * 12);
        c.lineTo(g.x - g.w / 2 + 12 + (g.w - 24) * (0.9 - i * 0.2), g.y - g.h / 2 + 18 + i * 12);
        c.stroke();
      }
      const sp = clamp((t - 0.3) / 0.25, 0, 1);
      if (sp > 0) {
        c.strokeStyle = C.shill;
        c.lineWidth = 2 / k;
        c.beginPath();
        const x0 = g.x - g.w / 2 - 6;
        const y0 = g.y + g.h / 2 + 4;
        c.moveTo(x0, y0);
        c.lineTo(x0 + (g.w + 12) * sp, y0 - (g.h + 8) * sp);
        c.stroke();
        c.font = `italic 400 ${13 / Math.sqrt(k)}px "EB Garamond", serif`;
        c.fillStyle = C.shill;
        c.textAlign = "center";
        c.globalAlpha = a * sp;
        c.fillText(g.label, g.x, g.y + g.h / 2 + 20);
      }
      c.restore();
    }
    this.ghosts = this.ghosts.filter((g) => now - g.start < g.dur);
  }

  /** compass circles: drawn as a sweep with the compass arm, then ticked, then fade */
  private drawOver(now: number, ox: number, oy: number) {
    const c = this.octx;
    const k = this.k;
    c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    c.clearRect(0, 0, this.W, this.H);
    if (!this.rings.length) return;
    c.setTransform(this.dpr * k, 0, 0, this.dpr * k, this.dpr * ox, this.dpr * oy);
    for (const r of this.rings) {
      const t = clamp((now - r.start) / r.dur, 0, 1);
      const sweep = clamp(t / 0.55, 0, 1);
      const e = 1 - Math.pow(1 - sweep, 2.4);
      const fade = t < 0.75 ? 1 : 1 - (t - 0.75) / 0.25;
      const R = lerp(r.r0 + 20, r.r1, 0.55);
      const a0 = -Math.PI / 2;
      c.globalAlpha = fade;
      c.strokeStyle = r.color;
      c.lineWidth = 1.4 / k;
      c.beginPath();
      c.arc(r.x, r.y, R, a0, a0 + e * Math.PI * 2);
      c.stroke();
      c.lineWidth = 0.8 / k;
      c.beginPath();
      c.arc(r.x, r.y, R + 10 / k, a0, a0 + e * Math.PI * 2);
      c.stroke();
      // ticks every 10 degrees
      const ticks = Math.floor(36 * e);
      for (let i = 0; i < ticks; i++) {
        const ang = a0 + (i / 36) * Math.PI * 2;
        const L = (i % 9 === 0 ? 14 : 6) / k;
        c.beginPath();
        c.moveTo(r.x + Math.cos(ang) * R, r.y + Math.sin(ang) * R);
        c.lineTo(r.x + Math.cos(ang) * (R - L), r.y + Math.sin(ang) * (R - L));
        c.stroke();
      }
      if (sweep < 1) {
        // the compass arm
        const ang = a0 + e * Math.PI * 2;
        c.setLineDash([3 / k, 4 / k]);
        c.beginPath();
        c.moveTo(r.x, r.y);
        c.lineTo(r.x + Math.cos(ang) * R, r.y + Math.sin(ang) * R);
        c.stroke();
        c.setLineDash([]);
      }
    }
    c.globalAlpha = 1;
    this.rings = this.rings.filter((r) => now - r.start < r.dur);
  }

  private pill(c: CanvasRenderingContext2D, x: number, y: number, text: string, col: string) {
    const k = this.k;
    const fs = 13.5 / Math.max(k, 0.7);
    c.font = `italic 400 ${fs}px "EB Garamond", serif`;
    const w = c.measureText(text).width + fs * 1.1;
    const h = fs * 1.55;
    c.save();
    c.fillStyle = this.colors.paper;
    c.strokeStyle = col;
    c.lineWidth = 1 / k;
    c.fillRect(x - w / 2, y - h / 2, w, h);
    c.globalAlpha = 0.7;
    c.strokeRect(x - w / 2, y - h / 2, w, h);
    c.globalAlpha = 1;
    c.fillStyle = col;
    c.textAlign = "center";
    c.textBaseline = "middle";
    c.fillText(text, x, y + fs * 0.06);
    c.restore();
  }

  /** bezier control points for an edge: [x0,y0,c1x,c1y,c2x,c2y,x1,y1] */
  private edgePts(e: VEdge): number[] {
    const a = e.a;
    const b = e.b;
    const ax = a.x + a.w / 2;
    const bx = b.x - b.w / 2;
    if (bx - ax > 30) {
      const dx = (bx - ax) * 0.5;
      return [ax, a.y, ax + dx, a.y, bx - dx, b.y, bx, b.y];
    }
    // vertical-ish: leave from top/bottom edge
    const down = b.y > a.y;
    const y0 = a.y + (down ? a.h / 2 : -a.h / 2);
    const y1 = b.y + (down ? -b.h / 2 : b.h / 2);
    const dy = (y1 - y0) * 0.5;
    if (Math.abs(b.y - a.y) < (a.h + b.h) / 2) {
      // overlapping rows: side to side
      const right = b.x > a.x;
      const x0 = a.x + (right ? a.w / 2 : -a.w / 2);
      const x1 = b.x + (right ? -b.w / 2 : b.w / 2);
      const d = (x1 - x0) * 0.5;
      return [x0, a.y, x0 + d, a.y, x1 - d, b.y, x1, b.y];
    }
    return [a.x, y0, a.x, y0 + dy, b.x, y1 - dy, b.x, y1];
  }

  private hatch: Record<string, CanvasPattern | null> = {};
  private hatchFor(c: CanvasRenderingContext2D, col: string) {
    if (this.hatch[col] !== undefined) return this.hatch[col];
    const t = document.createElement("canvas");
    t.width = t.height = 16;
    const x = t.getContext("2d")!;
    x.strokeStyle = col;
    x.globalAlpha = 0.5;
    x.lineWidth = 1;
    x.beginPath();
    for (let i = -16; i < 32; i += 8) {
      x.moveTo(i, 16);
      x.lineTo(i + 16, 0);
    }
    x.stroke();
    this.hatch[col] = c.createPattern(t, "repeat");
    return this.hatch[col];
  }

  private drawHull(c: CanvasRenderingContext2D, cl: Cluster, now: number, idx: number) {
    const ms = cl.members.map((id) => this.byId.get(id)!).filter((n) => n && n.vis);
    if (ms.length < 3) return;
    const pad = 28;
    const pts: [number, number][] = [];
    for (const n of ms) {
      const hw = n.w / 2 + pad;
      const hh = n.h / 2 + pad;
      pts.push([n.x - hw, n.y - hh], [n.x + hw, n.y - hh], [n.x + hw, n.y + hh], [n.x - hw, n.y + hh]);
    }
    const hull = convexHull(pts);
    const col = cl.status === "coordinated" ? this.colors.shill : cl.status === "likely_organic" ? this.colors.chill : this.colors.unsure;
    const k = this.k;
    const path = () => {
      c.beginPath();
      const n = hull.length;
      for (let i = 0; i < n; i++) {
        const p = hull[i];
        const q = hull[(i + 1) % n];
        const mx = (p[0] + q[0]) / 2;
        const my = (p[1] + q[1]) / 2;
        if (i === 0) c.moveTo(mx, my);
        else c.quadraticCurveTo(p[0], p[1], mx, my);
      }
      const p0 = hull[0];
      const p1 = hull[1 % n];
      c.quadraticCurveTo(p0[0], p0[1], (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2);
      c.closePath();
    };
    c.save();
    path();
    // engraving: hatched fill, scaled so the hatch stays fine on screen
    const pat = this.hatchFor(c, col);
    if (pat) {
      const m = new DOMMatrix().scale(1 / k, 1 / k);
      pat.setTransform(m);
      c.fillStyle = pat;
      c.globalAlpha = 0.32;
      c.fill();
    }
    c.globalAlpha = 0.85;
    c.strokeStyle = col;
    c.lineWidth = 1.2 / k;
    c.stroke();
    c.globalAlpha = 0.5;
    c.lineWidth = 0.7 / k;
    c.setLineDash([2 / k, 4 / k]);
    c.lineDashOffset = -now / 80 / k;
    c.translate(0, 0);
    c.restore();
    // outer dashed line, slowly marching
    c.save();
    c.strokeStyle = col;
    c.globalAlpha = 0.6;
    c.lineWidth = 0.8 / k;
    c.setLineDash([2 / k, 5 / k]);
    c.lineDashOffset = -now / 90 / k;
    const cxm = hull.reduce((s, p) => s + p[0], 0) / hull.length;
    const cym = hull.reduce((s, p) => s + p[1], 0) / hull.length;
    c.translate(cxm, cym);
    c.scale(1.035, 1.05);
    c.translate(-cxm, -cym);
    path();
    c.stroke();
    c.restore();
    // figure caption under the engraving
    let maxY = -Infinity;
    for (const p of hull) maxY = Math.max(maxY, p[1]);
    const xs = hull.map((p) => p[0]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const fs = 15 / Math.max(k, 0.12);
    c.save();
    c.textAlign = "center";
    c.textBaseline = "top";
    c.font = `500 ${fs * 0.82}px Cinzel, serif`;
    c.fillStyle = col;
    const fig = `FIG. ${["I", "II", "III", "IV", "V"][idx] ?? idx + 1} · ${STATUS[cl.status].short.toUpperCase()}`;
    c.fillText(fig, cx, maxY + 14 / k);
    c.font = `italic 400 ${fs}px "EB Garamond", serif`;
    c.fillStyle = this.colors.ink;
    c.fillText(`${cl.label}: ${cl.stat}`, cx, maxY + 14 / k + fs * 1.15);
    c.restore();
  }

  /** time-span label helper for UI */
  spanLabel(): string {
    if (!this.case) return "";
    const ts = this.case.nodes.map((n) => n.t);
    return fmtDur(Math.max(...ts) - Math.min(...ts));
  }
}

function convexHull(points: [number, number][]): [number, number][] {
  const p = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: number[], a: number[], b: number[]) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: [number, number][] = [];
  for (const pt of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], pt) <= 0) lower.pop();
    lower.push(pt);
  }
  const upper: [number, number][] = [];
  for (let i = p.length - 1; i >= 0; i--) {
    const pt = p[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], pt) <= 0) upper.pop();
    upper.push(pt);
  }
  upper.pop();
  lower.pop();
  return lower.concat(upper);
}
