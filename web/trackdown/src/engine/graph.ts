// Trackdown graph engine: DOM cards over canvas layers, custom force physics, inertial camera.
import type { Cluster, TraceCase, TraceEdge, TraceNode } from "../types";
import { cardHtml } from "../ui/card";
import backdropUrl from "../assets/venus-full.webp";
import { clusterShort, isLineageEdge, isMatchRel, relOf, ROLE, SANS } from "../ui/format";



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
  /** linked by a non-lineage relation (a screenshot of a post): drawn and highlighted, never part of the copy chain */
  aside: VNode[];
  asideOf: VNode | null; // for a screenshotted post, the post carrying the screenshot
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
  id: string;
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
  backdrop: HTMLElement;
  under: HTMLCanvasElement;
  over: HTMLCanvasElement;
  uctx: CanvasRenderingContext2D;
  octx: CanvasRenderingContext2D;
  W = 0;
  H = 0;
  dpr = 1;
  insets = { top: 52, bottom: 112, left: 24, right: 72 }; // ruler above, zoom controls on the right

  case: TraceCase | null = null;
  nodes: VNode[] = [];
  byId = new Map<string, VNode>();
  edges: VEdge[] = [];
  readonly mode = "timeline"; // the only layout since the Web layout was removed (#79); dock.ts still reads it
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
  private lastT = performance.now();

  constructor(stage: HTMLElement) {
    this.stage = stage;
    this.backdrop = document.createElement("div");
    this.backdrop.className = "backdrop";
    this.backdrop.style.backgroundImage = `url(${backdropUrl})`;
    stage.appendChild(this.backdrop);
    this.under = this.mkCanvas(1);
    this.world = document.createElement("div");
    this.world.className = "world";
    this.world.style.zIndex = "2";
    stage.appendChild(this.world);
    this.over = this.mkCanvas(4);
    this.uctx = this.under.getContext("2d")!;
    this.octx = this.over.getContext("2d")!;
    for (const r of Object.keys(ROLE)) this.colors[r] = ROLE[r as keyof typeof ROLE].color;
    this.colors.shill = cssVar("--shill");
    this.colors.chill = cssVar("--chill");
    this.colors.unsure = cssVar("--unsure");
    this.colors.ink = cssVar("--ink");
    this.colors.ink2 = cssVar("--ink-2");
    this.colors.ink3 = cssVar("--ink-3");
    this.colors.cv = cssVar("--cv");
    
    new ResizeObserver(() => this.resize()).observe(stage);
    this.resize();
    this.bind();
    requestAnimationFrame(this.frame);
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
    // A live lookup reloads the graph as sources arrive: keep already-decoded images so cards don't blink.
    const loaded = new Map<string, HTMLImageElement[]>();
    for (const img of this.world.querySelectorAll("img")) {
      if (!img.complete || !img.naturalWidth) continue;
      const list = loaded.get(img.src) ?? [];
      list.push(img);
      loaded.set(img.src, list);
    }
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
      const inSim = c.edges.find((e) => e.to === d.id && e.sim != null && isMatchRel(e.rel))?.sim;
      el.innerHTML = cardHtml(d, compact, order.get(d.id) ?? 0, inSim);
      for (const img of el.querySelectorAll("img")) {
        const old = loaded.get(img.src)?.pop();
        if (!old) continue;
        for (const a of img.getAttributeNames()) if (a !== "src") old.setAttribute(a, img.getAttribute(a)!);
        img.replaceWith(old);
      }
      if (inSim != null && d.media) el.classList.add("matched");
      this.world.appendChild(el);
      const n: VNode = { d, x: 0, y: 0, vx: 0, vy: 0, w: 0, h: 0, tx: 0, py: 0, el, vis: false, born: 0, compact, parents: [], children: [], aside: [], asideOf: null, tilt: 0, last: "", drag: false };
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
      if (isLineageEdge(e, a.d, b.d)) {
        a.children.push(b);
        b.parents.push(a);
      } else {
        a.aside.push(b);
        b.aside.push(a);
        b.asideOf ??= a;
      }
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
    const COLW = 268; // chip width 232 + a clear gutter
    for (const g of groups) {
      const gap = g[0].d.t - prevT;
      // at least ~170 px between moments, so a few posts minutes apart read left to right, not as a column
      const dx = gap <= 0 ? 0 : Math.max(170, Math.min(40 * Math.log2(1 + gap / 60000), 280 + 18 * Math.log2(1 + gap / 86400000)));
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
        // small groups: still ordered by real time (seconds apart read as distance), never one column
        g.forEach((n, i) => {
          if (i) x += Math.max(170, 90 * Math.log2(1 + (n.d.t - g[i - 1].d.t) / 10000));
          n.tx = x;
          xs.push(n.tx);
        });
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
    // a parentless aside post (the real post in a screenshot) is laid out next to the post carrying it
    const tucked = (n: VNode) => !n.parents.length && n.asideOf != null && n.asideOf !== n;
    const roots = this.nodes.filter((n) => !n.parents.length && !tucked(n));
    let slot = 0;
    const seen = new Set<VNode>();
    const gridTop = new Map<string, number>();
    const visit = (n: VNode): number => {
      if (seen.has(n)) return n.py;
      seen.add(n);
      const kids = [...n.children.filter((c) => c.parents[0] === n), ...n.aside.filter((c) => c.asideOf === n && tucked(c))].sort((a, b) => a.d.t - b.d.t);
      if (!kids.length) {
        const gr = this.grid.get(n);
        if (gr) {
          const RH = 90;
          let top = gridTop.get(gr.id);
          if (top == null) {
            top = slot;
            gridTop.set(gr.id, top);
            slot += gr.rows * RH + 30;
          }
          n.py = top + gr.row * RH + RH / 2;
          return n.py;
        }
        const gap = n.compact ? 90 : n.h + 40;
        n.py = slot + gap / 2;
        slot += gap;
        return n.py;
      }
      const ys = kids.map(visit);
      n.py = (Math.min(...ys) + Math.max(...ys)) / 2;
      return n.py;
    };
    roots.sort((a, b) => a.d.t - b.d.t).forEach(visit);
    // anything not reached (e.g. aside links in a loop) still gets a slot
    this.nodes.filter((n) => !seen.has(n)).sort((a, b) => a.d.t - b.d.t).forEach(visit);
    const mid = slot / 2;
    for (const n of this.nodes) n.py -= mid;
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
          const src = (from && this.byId.get(from)) || n.parents.find((p) => p.vis) || n.children.find((c) => c.vis) || n.aside.find((c) => c.vis);
          if (src) {
            n.x = src.x + (Math.random() - 0.5) * 30;
            n.y = src.y + (Math.random() - 0.5) * 30;
            n.vx = (n.tx - n.x) * 0.06;
            n.vy = (n.py - n.y) * 0.06;
          } else {
            n.x = n.tx;
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
        const f = (2600 * a) / Math.max(d2, 900);
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        p.vx -= fx * 0.15;
        p.vy -= fy;
        q.vx += fx * 0.15;
        q.vy += fy;
      }
    }
    // links
    for (const e of this.edges) {
      if (!e.a.vis || !e.b.vis) continue;
      const { a: p, b: q } = e;
      const dy = q.y - p.y;
      const k = 0.012 * a * (q.compact ? 0.5 : 1);
      p.vy += dy * k * 0.4;
      q.vy -= dy * k;
    }
    for (const n of ns) {
      n.vx += (n.tx - n.x) * 0.05 * Math.max(a, 0.25);
      n.vy += (n.py - n.y) * 0.004 * a;
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
          if (oy < ox * 1.6) {
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
    // a cluster box means "these are members": keep everyone else outside it
    for (const cl of this.case?.clusters ?? []) {
      const ms = cl.members.map((id) => this.byId.get(id)).filter((n): n is VNode => !!n && n.vis);
      if (ms.length < 3) continue;
      const inCl = new Set(ms);
      const gap = 26 + 24;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const m of ms) {
        x0 = Math.min(x0, m.x - m.w / 2);
        x1 = Math.max(x1, m.x + m.w / 2);
        y0 = Math.min(y0, m.y - m.h / 2);
        y1 = Math.max(y1, m.y + m.h / 2);
      }
      for (const n of ns) {
        if (inCl.has(n) || n.drag) continue;
        const l = n.x + n.w / 2 - (x0 - gap);
        const r = x1 + gap - (n.x - n.w / 2);
        const t = n.y + n.h / 2 - (y0 - gap - 24 / Math.max(this.k, 0.05)); // the tab sits above the box
        const b = y1 + gap - (n.y - n.h / 2);
        if (l <= 0 || r <= 0 || t <= 0 || b <= 0) continue;
        // in the timeline x is time, so prefer moving up or down
        const vert = Math.min(t, b);
        const horiz = Math.min(l, r) * 3;
        if (vert <= horiz) n.y += t < b ? -t * 0.5 : b * 0.5;
        else n.x += l < r ? -l * 0.5 : r * 0.5;
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
    // labels (~70px each side) and cluster tabs (~26px on top) are drawn in screen space around the boxes
    const aw = this.W - left - right - (this.W < 600 ? 12 + 40 : 40 + Math.min(160, this.W * 0.16 + 32));
    const ah = this.H - top - bottom - 40 - 40;
    // markers and labels are screen-size, so a wide timeline may zoom out further than cards could
    const k = clamp(Math.min(aw / (x1 - x0), ah / (y1 - y0)), 0.03, maxK);
    // shift centre so the box sits in the inset area
    const offX = (left - right) / 2 / k;
    const offY = (top + 26 - bottom) / 2 / k; // room for cluster tabs above the content
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
    // fit() may frame a long timeline below the 0.06 manual limit: never jump in from there
    this.zoomTarget = { k: clamp(base * f, Math.min(0.06, base), 2.6), sx, sy };
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
        this.k = clamp((this.pinch.k * d) / this.pinch.d, Math.min(0.06, this.pinch.k), 2.6);
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
    // aside links (a screenshot and the real post) light up one hop from the node itself only:
    // the real post is not part of anyone else's copy lineage
    for (const k of n.aside) out.add(k.d.id);
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
    this.rings.push({ id, color, start: performance.now(), dur, r0: Math.max(n.w, n.h) / 2, r1 });
  }

  ghost(nearId: string, count: number, label = "NO MATCH") {
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
    requestAnimationFrame(this.frame);
  };

  private render(now: number) {
    const k = this.k;
    const ox = this.W / 2 - this.cx * k;
    const oy = this.H / 2 - this.cy * k;
    // semantic zoom: cards stay up through normal zooming out; only near the zoom-out limit
    // (0.06x) do they fade into labelled markers (gone under 0.10x, full from 0.15x)
    const op = clamp((k - 0.1) / 0.05, 0, 1); // short crossfade: half-faded cards let lines show through
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
    this.drawOver(now);
  }

  private lastBackdrop = "";
  /** world-space boxes of the cluster tab labels drawn this frame (map labels avoid them) */
  private clusterTabs: { x0: number; y0: number; x1: number; y1: number }[] = [];

  private drawUnder(now: number, ox: number, oy: number, op: number) {
    const c = this.uctx;
    const k = this.k;
    const dpr = this.dpr;
    const C = this.colors;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, this.W, this.H);
    // the painting drifts slowly behind the graph (parallax), like looking through the work
    const px = clamp(-this.cx * 0.04, -60, 60);
    const py = clamp(-this.cy * 0.04, -40, 40);
    const tf = `translate(${px.toFixed(1)}px, ${py.toFixed(1)}px) scale(${(1 + Math.min(0.12, Math.log2(Math.max(k, 0.1) / 0.1) * 0.02)).toFixed(3)})`;
    if (tf !== this.lastBackdrop) {
      this.backdrop.style.transform = tf;
      this.lastBackdrop = tf;
    }
    // faint registration crosses
    let step = 192;
    while (step * k < 80) step *= 2;
    const gx0 = Math.floor(-ox / k / step) * step;
    const gy0 = Math.floor(-oy / k / step) * step;
    c.strokeStyle = "rgba(255,255,255,0.10)";
    c.lineWidth = 1;
    c.beginPath();
    for (let x = gx0; x * k + ox < this.W; x += step)
      for (let y = gy0; y * k + oy < this.H; y += step) {
        const sx = Math.round(x * k + ox) + 0.5;
        const sy = Math.round(y * k + oy) + 0.5;
        c.moveTo(sx - 4, sy);
        c.lineTo(sx + 4, sy);
        c.moveTo(sx, sy - 4);
        c.lineTo(sx, sy + 4);
      }
    c.stroke();
    c.setTransform(dpr * k, 0, 0, dpr * k, dpr * ox, dpr * oy);
    // territories
    this.clusterTabs = [];
    if (this.case) this.case.clusters.forEach((cl) => this.drawHull(c, cl, now, op));
    // links
    const hl = this.hl;
    c.lineCap = "round";
    for (const e of this.edges) {
      if (!e.a.vis || !e.b.vis) continue;
      // in map mode the cluster hull stands for its members: no spokes to every dot
      // (they come back on hover/selection and once cards are readable)
      if (op < 1 && e.b.compact && e.b.d.cluster && !(hl && hl.has(e.a.d.id) && hl.has(e.b.d.id))) continue;
      const pts = this.edgePts(e, op);
      const prog = clamp((now - e.born) / 650, 0, 1);
      const on = hl ? hl.has(e.a.d.id) && hl.has(e.b.d.id) : false;
      const dim = hl && !on;
      const match = isMatchRel(e.e.rel);
      const shot = e.e.rel === "screenshot_of";
      const role = this.colors[e.b.d.role] ?? C.ink2;
      const col = on ? role : e.e.rel === "variant" ? "#a2ffb5" : e.e.rel === "history" ? "rgba(228,179,106,0.9)" : e.e.rel === "derived" ? "rgba(217,120,61,0.85)" : match ? "rgba(245,245,245,0.9)" : shot ? ROLE.screenshotted.color : e.e.rel === "cites" ? "rgba(122,162,240,0.85)" : "rgba(242,239,233,0.55)";
      c.save();
      c.globalAlpha = dim ? 0.1 : on ? 1 : match ? 0.75 : 0.7;
      c.strokeStyle = col;
      c.lineWidth = (on ? 2.2 : match ? 1.1 : e.e.rel === "history" ? 3 : e.e.rel === "derived" ? 2 : 1.3) / k;
      if (match || e.e.rel === "cites") c.setLineDash([7 / k, 6 / k]);
      else if (shot) c.setLineDash([2 / k, 5 / k]);
      else if (e.e.rel === "variant") c.setLineDash(relOf("variant").dash.map((length) => length / k));
      if (match) c.lineDashOffset = -now / 40 / k;
      c.beginPath();
      c.moveTo(pts[0], pts[1]);
      if (prog >= 1) c.bezierCurveTo(pts[2], pts[3], pts[4], pts[5], pts[6], pts[7]);
      else
        for (let i = 1, N = 28; i <= N * prog; i++) {
          const t = i / N;
          c.lineTo(bez(pts[0], pts[2], pts[4], pts[6], t), bez(pts[1], pts[3], pts[5], pts[7], t));
        }
      c.stroke();
      c.restore();
      if (prog < 1) {
        const hx = bez(pts[0], pts[2], pts[4], pts[6], prog);
        const hy = bez(pts[1], pts[3], pts[5], pts[7], prog);
        c.strokeStyle = C.cv;
        c.lineWidth = 1.5 / k;
        const s = 9 / k;
        c.strokeRect(hx - s / 2, hy - s / 2, s, s);
        continue;
      }
      if (dim) continue;
      if (match && op >= 1) {
        // detection boxes at both ends, like matched keypoints
        c.strokeStyle = C.cv;
        c.lineWidth = 1.5 / k;
        const s = (on ? 12 : 9) / k;
        c.strokeRect(pts[0] - s / 2, pts[1] - s / 2, s, s);
        c.strokeRect(pts[6] - s / 2, pts[7] - s / 2, s, s);
      } else if (!match) {
        // arrowhead
        const tx = pts[6] - bez(pts[0], pts[2], pts[4], pts[6], 0.97);
        const ty = pts[7] - bez(pts[1], pts[3], pts[5], pts[7], 0.97);
        const L = Math.hypot(tx, ty) || 1;
        const ux = tx / L;
        const uy = ty / L;
        const s = (on ? 8 : 6) / k;
        c.fillStyle = col;
        c.globalAlpha = 0.85;
        c.beginPath();
        c.moveTo(pts[6], pts[7]);
        c.lineTo(pts[6] - ux * s - uy * s * 0.5, pts[7] - uy * s + ux * s * 0.5);
        c.lineTo(pts[6] - ux * s + uy * s * 0.5, pts[7] - uy * s - ux * s * 0.5);
        c.closePath();
        c.fill();
        c.globalAlpha = 1;
      }
      // pulses travelling parent -> child
      const n = on ? 3 : 1;
      const speed = on ? 0.0005 : 0.0002;
      c.fillStyle = on ? role : "#f2efe9";
      for (let i = 0; i < n; i++) {
        const u = (now * speed + e.seed + i / n) % 1;
        c.globalAlpha = (on ? 0.95 : 0.55) * Math.sin(u * Math.PI);
        c.beginPath();
        c.arc(bez(pts[0], pts[2], pts[4], pts[6], u), bez(pts[1], pts[3], pts[5], pts[7], u), (on ? 2.8 : 1.9) / k, 0, Math.PI * 2);
        c.fill();
      }
      c.globalAlpha = 1;
      if (k > 0.45 && op > 0.5 && (e.e.sim != null || e.e.rel === "cites" || e.e.label) && (!e.b.compact || on)) {
        const mx = bez(pts[0], pts[2], pts[4], pts[6], 0.5);
        const my = bez(pts[1], pts[3], pts[5], pts[7], 0.5);
        // a score tag half under a card reads as a glitch: skip it
        const text = e.e.label ?? (e.e.sim != null ? e.e.sim.toFixed(2) : "Cites");
        const cap = !e.e.label && e.e.sim != null ? relOf(e.e.rel).label : "";
        const fs = 10.5 / Math.max(k, 0.65);
        c.font = `600 ${fs}px ${SANS}`;
        const pw = c.measureText(text + cap).width + fs * 2;
        const ph = fs * 1.7;
        const covered = this.nodes.some((n) => n.vis && Math.abs(n.x - mx) < n.w / 2 + pw / 2 && Math.abs(n.y - my) < n.h / 2 + ph / 2);
        if (!covered) this.pill(c, mx, my, text, shot ? ROLE.screenshotted.color : e.e.label ? (/NEW/.test(e.e.label) ? "#e2533c" : e.e.rel === "history" ? "#e4b36a" : "#d9783d") : e.e.sim != null ? C.cv : "#5b8fe6", cap);
      }
    }
    // far zoom: cities on the map
    if (op < 1) {
      const labels: { n: VNode; r: number; text: string }[] = [];
      for (const n of this.nodes) {
        if (!n.vis) continue;
        const col = this.colors[n.d.role] ?? C.ink2;
        const big = n.d.role === "origin" || n.d.role === "submitted";
        const r = this.markerR(n);
        const dim = hl && !hl.has(n.d.id);
        const lit = n.d.id === this.hoverId || n.d.id === this.selId;
        c.globalAlpha = clamp(1 - op * 1.7, 0, 1) * (dim ? 0.25 : 1);
        c.fillStyle = col;
        if (n.compact && !lit) {
          // a cluster member: a small square marker; the hull around them carries the label and count
          c.fillRect(n.x - r * 0.6, n.y - r * 0.6, r * 1.2, r * 1.2);
          c.globalAlpha *= 0.55;
          c.strokeStyle = col;
          c.lineWidth = 1 / k;
          c.strokeRect(n.x - r, n.y - r, r * 2, r * 2);
          continue;
        }
        c.fillRect(n.x - r * 0.6, n.y - r * 0.6, r * 1.2, r * 1.2);
        c.strokeStyle = lit ? C.cv : col;
        c.lineWidth = (lit ? 2 : 1.4) / k;
        c.globalAlpha *= lit || big ? 1 : 0.7;
        c.strokeRect(n.x - r * 1.3, n.y - r * 1.3, r * 2.6, r * 2.6);
        if (big || n.d.role === "mutation" || n.d.id === this.hoverId || n.d.id === this.selId)
          labels.push({ n, r, text: n.d.role === "origin" ? "Earliest known" : n.d.role === "submitted" ? "Submitted" : n.d.author.name });
      }
      // labels: most important first; one that would overlap a placed label (or a cluster tab) is dropped
      const rank = (n: VNode) => (n.d.id === this.selId ? 0 : n.d.id === this.hoverId ? 1 : n.d.role === "submitted" ? 2 : n.d.role === "origin" ? 3 : 4);
      labels.sort((a, b) => rank(a.n) - rank(b.n));
      const placed = this.clusterTabs.slice();
      const placedText = new Set<string>();
      // markers count as occupied too, so a label never sits on another post
      const markers = labels.length ? this.nodes.filter((n) => n.vis).map((n) => {
        const r = this.markerR(n) * 1.3;
        return { id: n.d.id, x0: n.x - r, x1: n.x + r, y0: n.y - r, y1: n.y + r };
      }) : [];
      c.font = `600 ${(this.W < 600 ? 11 : 12.5) / k}px ${SANS}`;
      c.textAlign = "center";
      c.textBaseline = "alphabetic";
      c.lineJoin = "round";
      for (const l of labels) {
        const w = c.measureText(l.text).width;
        const h = 15 / k;
        // above the marker, else below it, else not at all
        const over = (b: { x0: number; x1: number; y0: number; y1: number }, p: { x0: number; x1: number; y0: number; y1: number }) => b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0;
        const must = rank(l.n) <= 3; // selected, hovered, submitted and earliest always get a label
        const hitsLabel = (b: { x0: number; x1: number; y0: number; y1: number }) => placed.some((p) => over(b, p));
        const hits = (b: { x0: number; x1: number; y0: number; y1: number }) => hitsLabel(b) || markers.some((m) => m.id !== l.n.d.id && over(b, m));
        const edgeL = (0 - this.W / 2) / k + this.cx;
        const edgeR = (this.W - this.W / 2) / k + this.cx;
        if (l.n.x < edgeL || l.n.x > edgeR) continue; // marker off screen: no orphan label
        if (placedText.has(l.text)) continue; // two identical labels side by side read as a glitch
        const vl = edgeL + w / 2 + 12 / k;
        const vr = edgeR - w / 2 - 12 / k;
        let lx = vl < vr ? clamp(l.n.x, vl, vr) : l.n.x; // slide back inside the view, never clip
        const at = (y: number, cx = lx) => ({ y, cx, x0: cx - w / 2 - 3 / k, x1: cx + w / 2 + 3 / k, y0: y - h, y1: y + 3 / k });
        // above, below, left, right of the marker; only a must-show label may finally cover a marker
        const mid = l.n.y + h / 2 - 2 / k;
        const side = l.r * 1.3 + 8 / k + w / 2;
        const spots = [at(l.n.y - l.r * 1.3 - 6 / k), at(l.n.y + l.r * 1.3 + h + 2 / k), at(mid, l.n.x - side), at(mid, l.n.x + side)].filter(
          (b) => b.x0 >= edgeL + 10 / k && b.x1 <= edgeR - 10 / k,
        );
        let box = spots.find((b) => !hits(b));
        if (!box) {
          if (!must) continue;
          // may cover a marker, never another label: pick the spot that covers the least
          const cover = (b: { x0: number; x1: number; y0: number; y1: number }) =>
            markers.reduce((s, m) => (m.id === l.n.d.id ? s : s + Math.max(0, Math.min(b.x1, m.x1) - Math.max(b.x0, m.x0)) * Math.max(0, Math.min(b.y1, m.y1) - Math.max(b.y0, m.y0))), 0);
          box = spots.filter((b) => !hitsLabel(b)).sort((a, b) => cover(a) - cover(b))[0] ?? at(l.n.y - l.r * 1.3 - 6 / k);
        }
        lx = box.cx;
        placedText.add(l.text);
        const y = box.y;
        placed.push(box);
        c.globalAlpha = hl && !hl.has(l.n.d.id) ? 0.35 : clamp(1 - op * 1.7, 0, 1);
        c.strokeStyle = "rgba(5,5,6,0.9)";
        c.lineWidth = 4 / k;
        c.strokeText(l.text, lx, y);
        c.fillStyle = "#f2efe9";
        c.fillText(l.text, lx, y);
      }
      c.globalAlpha = 1;
    }
    this.drawTabs(c);
    // rejected candidates: red detection boxes
    for (const g of this.ghosts) {
      const t = (now - g.start) / g.dur;
      if (t < 0 || t > 1) continue;
      const a = t < 0.12 ? t / 0.12 : 1 - (t - 0.12) / 0.88;
      const s = 1.25 - 0.25 * Math.min(1, t * 4);
      const w = g.w * s;
      const h = g.h * s;
      c.save();
      c.globalAlpha = a * 0.9;
      c.strokeStyle = C.shill;
      c.lineWidth = 1.4 / k;
      c.setLineDash([5 / k, 4 / k]);
      c.strokeRect(g.x - w / 2, g.y - h / 2, w, h);
      c.setLineDash([]);
      c.fillStyle = "rgba(210,68,47,0.06)";
      c.fillRect(g.x - w / 2, g.y - h / 2, w, h);
      if (t > 0.25) {
        c.beginPath();
        c.moveTo(g.x - w / 2, g.y - h / 2);
        c.lineTo(g.x + w / 2, g.y + h / 2);
        c.stroke();
        c.font = `600 ${11 / Math.sqrt(k)}px ${SANS}`;
        c.fillStyle = C.shill;
        c.textAlign = "left";
        c.fillText(g.label, g.x - w / 2, g.y - h / 2 - 6);
      }
      c.restore();
    }
    this.ghosts = this.ghosts.filter((g) => now - g.start < g.dur);
  }

  /** machine-vision lock-on: corner brackets close in on the card, crosshair sweeps */
  private drawOver(now: number) {
    const c = this.octx;
    const k = this.k;
    c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    c.clearRect(0, 0, this.W, this.H);
    if (!this.rings.length) return;
    for (const r of this.rings) {
      const n = this.byId.get(r.id);
      if (!n) continue;
      const t = clamp((now - r.start) / r.dur, 0, 1);
      const e = 1 - Math.pow(1 - clamp(t / 0.45, 0, 1), 3);
      const fade = t < 0.7 ? 1 : 1 - (t - 0.7) / 0.3;
      const sx = (n.x - this.cx) * k + this.W / 2;
      const sy = (n.y - this.cy) * k + this.H / 2;
      const w0 = (n.w * k) / 2 + 8;
      const h0 = (n.h * k) / 2 + 8;
      const grow = lerp(90, 0, e);
      const hw = w0 + grow;
      const hh = h0 + grow;
      c.globalAlpha = fade;
      c.strokeStyle = r.color;
      c.lineWidth = 2;
      const L = Math.min(22, hw * 0.4);
      for (const [dx, dy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]] as const) {
        const x = sx + dx * hw;
        const y = sy + dy * hh;
        c.beginPath();
        c.moveTo(x, y - dy * L);
        c.lineTo(x, y);
        c.lineTo(x - dx * L, y);
        c.stroke();
      }
      // crosshair sweep
      c.globalAlpha = fade * (1 - e) * 0.8;
      c.lineWidth = 1;
      c.setLineDash([3, 5]);
      c.beginPath();
      c.moveTo(0, sy);
      c.lineTo(this.W, sy);
      c.moveTo(sx, 0);
      c.lineTo(sx, this.H);
      c.stroke();
      c.setLineDash([]);
    }
    c.globalAlpha = 1;
    this.rings = this.rings.filter((r) => now - r.start < r.dur);
  }

  /** Link score and caption on a neutral backing for readability. */
  private pill(c: CanvasRenderingContext2D, x: number, y: number, text: string, col: string, cap = "") {
    const k = this.k;
    const fs = 10.5 / Math.max(k, 0.65);
    c.font = `600 ${fs}px ${SANS}`;
    const w1 = c.measureText(text).width;
    c.font = `600 ${fs * 0.72}px ${SANS}`;
    const w2 = cap ? c.measureText(cap).width + fs * 0.6 : 0;
    const w = w1 + w2 + fs * 1.1;
    const h = fs * 1.7;
    c.save();
    c.fillStyle = "rgba(6,6,8,0.92)";
    c.fillRect(x - w / 2, y - h / 2, w, h);
    c.textBaseline = "middle";
    c.textAlign = "left";
    let tx = x - w / 2 + fs * 0.55;
    if (cap) {
      c.fillStyle = "rgba(242,239,233,0.55)";
      c.fillText(cap, tx, y + fs * 0.05);
      tx += w2;
    }
    c.font = `600 ${fs}px ${SANS}`;
    c.fillStyle = col;
    c.fillText(text, tx, y + fs * 0.05);
    c.restore();
  }

  /** half-size of a node's map marker, in world units */
  private markerR(n: VNode): number {
    const big = n.d.role === "origin" || n.d.role === "submitted";
    // phones fit a long timeline into a narrow view: smaller markers keep posts apart
    return ((big ? 9 : n.compact ? 4.5 : 6.5) * (this.W < 600 ? 0.7 : 1)) / this.k;
  }

  /** bezier control points for an edge: [x0,y0,c1x,c1y,c2x,c2y,x1,y1]. `op` blends the anchors
   *  from the card's edge (cards shown) to the marker's edge (map mode). */
  private edgePts(e: VEdge, op = 1): number[] {
    const box = (n: VNode) => {
      const m = this.markerR(n) * (n.compact ? 0.5 : 1.3);
      return { x: n.x, y: n.y, w: lerp(m * 2, n.w, op), h: lerp(m * 2, n.h, op) };
    };
    const a = box(e.a);
    const b = box(e.b);
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

  /** A cluster is drawn as one big detection box around its members, labelled like the
   *  machine-vision boxes in the Junction banner: name + numbers on a solid tab. */
  private drawHull(c: CanvasRenderingContext2D, cl: Cluster, now: number, op = 1) {
    const ms = cl.members.map((id) => this.byId.get(id)!).filter((n) => n && n.vis);
    if (ms.length < 3) return;
    // wrap what is drawn: card bounds when cards show, marker bounds on the map (blended by op)
    const pad = lerp(14 / this.k, 26, op);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ms) {
      const m = this.markerR(n) * 1.3;
      const hw = lerp(m, n.w / 2, op);
      const hh = lerp(m, n.h / 2, op);
      x0 = Math.min(x0, n.x - hw - pad);
      x1 = Math.max(x1, n.x + hw + pad);
      y0 = Math.min(y0, n.y - hh - pad);
      y1 = Math.max(y1, n.y + hh + pad);
    }
    const col = cl.status === "coordinated" ? this.colors.shill : cl.status === "likely_organic" ? this.colors.chill : this.colors.unsure;
    const k = this.k;
    c.save();
    c.fillStyle = col;
    c.globalAlpha = 0.05 + 0.02 * Math.sin(now / 900);
    c.fillRect(x0, y0, x1 - x0, y1 - y0);
    c.globalAlpha = 0.9;
    c.strokeStyle = col;
    c.lineWidth = 2 / k;
    c.strokeRect(x0, y0, x1 - x0, y1 - y0);
    // heavier corners
    const L = Math.min(40 / k, (x1 - x0) / 4);
    c.lineWidth = 4 / k;
    c.beginPath();
    for (const [cx, cy, dx, dy] of [[x0, y0, 1, 1], [x1, y0, -1, 1], [x1, y1, -1, -1], [x0, y1, 1, -1]] as const) {
      c.moveTo(cx, cy + dy * L);
      c.lineTo(cx, cy);
      c.lineTo(cx + dx * L, cy);
    }
    c.stroke();
    // tab label on the top edge, like a detector's class + score
    const fs = 11 / k; // constant on screen: the verdict label must never become microtype
    c.font = `600 ${fs}px ${SANS}`;
    // the tab never runs past its box: drop detail from the right until it fits
    const parts = [clusterShort(cl.status, !!this.case?.lookup), ...cl.stat.split(" · ")];
    let label = parts.join(" · ");
    while (parts.length > 2 && c.measureText(label).width + fs * 1.2 > x1 - x0) { // always keep verdict + count
      parts.pop();
      label = parts.join(" · ");
    }
    const w = c.measureText(label).width + fs * 1.2;
    const h = fs * 1.9;
    c.restore();
    // slide the tab right so it stays readable while any of the box is on screen
    const viewL = (0 - this.W / 2) / k + this.cx + 8 / k;
    const tx = clamp(viewL, x0, Math.max(x0, x1 - w));
    // a marker under the tab would hide the verdict: then hang the tab under the box instead
    const inCl = new Set(ms);
    const drawnBox = (n: VNode) => {
      const m = this.markerR(n) * 1.3;
      return { hw: lerp(m, n.w / 2, op), hh: lerp(m, n.h / 2, op) };
    };
    const blocked = (ty: number) =>
      this.nodes.some((n) => {
        if (!n.vis || inCl.has(n)) return false;
        const { hw, hh } = drawnBox(n);
        return n.x + hw > tx && n.x - hw < tx + w && n.y + hh > ty && n.y - hh < ty + h;
      });
    const ty = blocked(y0 - h) && !blocked(y1) ? y1 : y0 - h;
    this.clusterTabs.push({ x0: tx, y0: ty, x1: tx + w, y1: ty + h });
    this.pendingTabs.push({ label, col, x: tx, y: ty, w, h, fs });
  }

  /** cluster tabs go on top of everything else on the canvas */
  private pendingTabs: { label: string; col: string; x: number; y: number; w: number; h: number; fs: number }[] = [];
  private drawTabs(c: CanvasRenderingContext2D) {
    c.save();
    for (const t of this.pendingTabs) {
      c.globalAlpha = 1;
      c.fillStyle = "rgba(6,6,8,0.92)";
      c.fillRect(t.x - 1 / this.k, t.y, t.w, t.h);
      c.font = `600 ${t.fs}px ${SANS}`;
      c.fillStyle = t.col;
      c.textAlign = "left";
      c.textBaseline = "middle";
      c.fillText(t.label, t.x + t.fs * 0.6, t.y + t.h / 2 + t.fs * 0.05);
    }
    c.restore();
    this.pendingTabs = [];
  }
}
