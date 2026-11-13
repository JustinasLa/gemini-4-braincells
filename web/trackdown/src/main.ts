import "./styles.css";
import rhinoUrl from "./assets/rhino-1515.webp";
import { analyze, listLive, liveCase, type ClusterSummary } from "./data/api";
import { simulatedCases } from "./data/cases";
import { simulateFromInput } from "./data/simulate";
import { Graph, type Mode } from "./engine/graph";
import type { TraceCase } from "./types";
import { coinImg, flipInto, preloadCoin, wait } from "./ui/coin";
import { bannerReady, mountBanner } from "./ui/cvbanner";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtClock, fmtDate, fmtDur, fmtShort, ROLE, STATUS, TOOL } from "./ui/format";
import { icon } from "./ui/icons";
import { Panel } from "./ui/panel";
import { initTooltips } from "./ui/tooltip";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;
const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <div class="brand">
      <button class="btn icon mobile-only rail-btn" data-act="rail" aria-label="Case files">${icon.menu}</button>
      <div class="emblem" style="background-image:url(${coinImg("coordinated")})"></div>
      <div class="wordmark">SHILL OR CHILL<small>TRACKDOWN · JUNCTION 2026</small></div>
    </div>
    <div class="resources" id="res"></div>
    <div class="clockbox">
      <button class="pp" id="pp" aria-label="Play or pause" data-tip="<h4>Replay the spread</h4>Space to play or pause.">${icon.play}</button>
      <div class="pips" id="pips" data-tip="<h4>Game speed</h4>How fast the replay runs.">${[1, 2, 3, 4, 5].map((i) => `<button data-sp="${i}"></button>`).join("")}</div>
      <div class="date" id="date">–<small>–</small></div>
    </div>
    <div class="top-actions">
      <div class="seg" id="mode"><button data-mode="timeline" class="on" data-tip="<h4>Timeline</h4>Cards sit at their moment in time (T).">Timeline</button><button data-mode="web" data-tip="<h4>Web</h4>Free force layout (T).">Web</button></div>
      <button class="btn icon mobile-only" data-act="sheet" aria-label="Attribution">${icon.receipt}</button>
      <button class="btn gold" data-act="trace">${icon.search}<span class="lbl">Trace a post</span><span class="kbd">N</span></button>
    </div>
  </header>
  <aside class="rail"><div class="rail-inner" id="rail"></div></aside>
  <main class="stage" id="stage">
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="term hidden" id="console" data-ui></div>
    <div class="hint" id="hint">Drag cards · scroll to zoom · double-click to focus</div>
    <div class="legend gilt min" id="legend" data-ui></div>
    <div class="hud" data-ui>
      <div class="zoom"><button data-z="in" aria-label="Zoom in">${icon.plus}</button><button data-z="out" aria-label="Zoom out">${icon.minus}</button><button data-z="fit" aria-label="Fit">${icon.fit}</button></div>
      <div class="minimap gilt hud-corners" id="minimap"><span class="mm-title">THEATRE</span></div>
    </div>
    <div class="dock gilt" id="dock" data-ui></div>
    <div class="vignette"></div>
    <div class="scanlines"></div>
  </main>
  <aside class="panel" id="panel"></aside>
</div>`;

const app = $(".app");
const stage = $("#stage");
const g = new Graph(stage);
(window as unknown as { __g: Graph }).__g = g;
const panel = new Panel($("#panel"), g);
const dock = new Dock($("#dock"), g);
const mini = new Minimap($("#minimap"), g);
const ruler = $<HTMLCanvasElement>("#ruler");
const consoleEl = $("#console");
let current: TraceCase | null = null;
let tracing = 0;
let skipFlag = false;
let liveRows: ClusterSummary[] = [];
preloadCoin();
initTooltips();

// ---------------------------------------------------------------- legend
$("#legend").innerHTML =
  `<div class="legend-title"><span>Map key</span><span>+</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="ln" style="--s:dashed;--c:#f0ece2"></span>Match (copy, image)</div><div class="li"><span class="ln" style="--c:#ebe3d1"></span>Repost, quote</div><div class="li"><span class="ln" style="--s:dashed;--c:#5b8fe6"></span>Cites</div><div class="li"><span class="sw" style="--c:#3fe07a"></span>Machine detection</div>`;
$("#legend").addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest(".legend-title")) {
    const l = $("#legend");
    l.classList.toggle("min");
    $(".legend-title span:last-child", l).textContent = l.classList.contains("min") ? "+" : "–";
  }
});

// ---------------------------------------------------------------- resource bar (Victoria-style)
const RI = {
  posts: icon.bluesky,
  accounts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.6c2.6.2 4.6 1.9 5.3 5.4"/></svg>',
  hops: icon.shuffle,
  span: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/></svg>',
  clusters: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6l6-2 6 2 4-1v13l-4 1-6-2-6 2-0 0V6z"/><path d="M10 4v14M16 6v14"/></svg>',
  verdict: icon.crown,
};
function renderResources(c: TraceCase) {
  const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
  const fresh = c.nodes.filter((n) => (n.author.ageDays ?? 999) < 30).length;
  const hops = Math.max(0, g.pathToOrigin(c.submittedId).length - 1);
  const ts = c.nodes.map((n) => n.t);
  const span = Math.max(...ts) - Math.min(...ts);
  const v = STATUS[c.status];
  const cls = c.status === "coordinated" ? "shill" : c.status === "likely_organic" ? "chill" : "unsure";
  const tag = c.synthetic ? '<span class="flagtag sim">SIMULATED</span>' : c.historical ? '<span class="flagtag hist">HISTORICAL</span>' : c.live ? '<span class="flagtag live">LIVE</span>' : "";
  const r = (ic: string, val: string, label: string, tip: string, extra = "") =>
    `<div class="res" data-tip="${esc(tip)}"><span class="ri">${ic}</span><span><div class="rv ${extra}">${val}</div><div class="rl">${label}</div></span></div>`;
  $("#res").innerHTML =
    `<div class="case-title"><b>${esc(c.title)}</b><span>${esc(c.kicker)} ${tag}</span></div>` +
    r(RI.verdict, v.short.toUpperCase(), "Verdict", `<h4>${v.word}</h4>${v.tag}.${c.scored === false || c.status === "insufficient" ? " No confidence score." : `<div class='row'><span>Confidence</span><b>${Math.round(c.confidence * 100)}%</b></div>`}`, cls) +
    r(RI.posts, String(c.nodes.length), "Posts", `<h4>Posts on the map</h4><div class='row'><span>Total</span><b>${c.nodes.length}</b></div><div class='row'><span>Found by search</span><b>${c.nodes.filter((n) => n.role === "source").length}</b></div>`) +
    r(RI.accounts, String(accounts), c.historical ? "Hands" : "Accounts", `<h4>${c.historical ? "Hands" : "Accounts"}</h4><div class='row'><span>Distinct</span><b>${accounts}</b></div><div class='row'><span>Under 30 days old</span><b class='${fresh ? "neg" : ""}'>${fresh}</b></div>`) +
    r(RI.hops, String(hops), "Copies back", `<h4>Copies back</h4>Hops from the submitted post to the earliest copy.`) +
    r(RI.span, fmtDur(span), "Span", `<h4>Span</h4>First to last post on the map.`) +
    r(RI.clusters, String(c.clusters.length), "Territories", `<h4>Territories</h4>${c.clusters.length ? c.clusters.map((cl) => `<div class='row'><span>${esc(cl.label)}</span><b>${STATUS[cl.status].short}</b></div>`).join("") : "No cluster large enough to draw."}`);
  document.title = `${c.title} · Shill or Chill`;
}

// ---------------------------------------------------------------- clock + speed (EU4)
const SPEEDS = [0.25, 0.5, 1, 2, 4];
let speedIx = 2;
function setSpeed(i: number) {
  speedIx = Math.max(0, Math.min(4, i));
  dock.speed = SPEEDS[speedIx];
  document.querySelectorAll<HTMLElement>("#pips button").forEach((b, j) => b.classList.toggle("on", j <= speedIx));
}
setSpeed(2);
$("#pips").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-sp]");
  if (b) setSpeed(Number(b.dataset.sp) - 1);
});
$("#pp").addEventListener("click", () => dock.toggle());
let lastDate = "";
function updateClock() {
  const c = current;
  if (!c) return;
  const tm = g.timeMap;
  const first = tm[0]?.[0];
  const last = tm[tm.length - 1]?.[0];
  const t = dock.u >= 0.9999 ? last : dock.timeForU(dock.u);
  if (t == null || first == null) return;
  const s = `${fmtDate(t, false)}${new Date(t).getUTCFullYear() >= 1900 ? ` · ${fmtClock(t)}` : ""}|T+${fmtDur(t - first)} since origin`;
  if (s === lastDate) return;
  lastDate = s;
  const [a, b] = s.split("|");
  $("#date").innerHTML = `${esc(a)}<small>${esc(b)}</small>`;
  $("#pp").innerHTML = dock.playing ? icon.pause : icon.play;
}

// ---------------------------------------------------------------- outliner
function spark(c: TraceCase, canvas: HTMLCanvasElement) {
  const dpr = Math.min(2, devicePixelRatio || 1);
  const w = 200;
  const h = 14;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const x = canvas.getContext("2d")!;
  x.scale(dpr, dpr);
  const ts = c.nodes.map((n) => n.t).sort((a, b) => a - b);
  const xs: number[] = [];
  let acc = 0;
  ts.forEach((t, i) => {
    if (i) acc += Math.log2(1 + (t - ts[i - 1]) / 60000);
    xs.push(acc);
  });
  const max = acc || 1;
  const bins = new Array(40).fill(0);
  xs.forEach((v) => bins[Math.min(39, Math.floor((v / max) * 39))]++);
  const m = Math.max(...bins);
  x.fillStyle = "rgba(201,166,94,0.3)";
  x.fillRect(0, h - 1, w, 1);
  bins.forEach((b, i) => {
    if (!b) return;
    const bh = Math.max(2, Math.sqrt(b / m) * (h - 2));
    x.fillStyle = STATUS[c.status].color;
    x.globalAlpha = 0.45 + 0.55 * (b / m);
    x.fillRect(i * 5, h - 1 - bh, 3, bh);
  });
}

function railItem(c: TraceCase) {
  const sub = c.nodes.find((n) => n.id === c.submittedId);
  const ori = c.nodes.find((n) => n.id === c.originId);
  return `<button class="cat-item${current?.id === c.id ? " on" : ""}" data-case="${c.id}" style="--vc:${STATUS[c.status].css}">
    <span class="mini-coin" style="background-image:url(${coinImg(c.status)})"></span>
    <span class="vd">${STATUS[c.status].short} · ${esc(c.kicker)}</span>
    <span class="ttl">${esc(c.title)}</span>
    <span class="meta">${ori ? fmtShort(ori.t) : ""} → ${sub ? fmtShort(sub.t) : ""}${sub && ori ? ` · ${fmtDur(sub.t - ori.t)}` : ""}</span>
    <canvas class="spark"></canvas>
  </button>`;
}

const USER_KEY = "soc-trackdown-traces";
const userCases: TraceCase[] = (() => {
  try {
    return (JSON.parse(localStorage.getItem(USER_KEY) ?? "[]") as TraceCase[]).slice(0, 8);
  } catch {
    return [];
  }
})();
function saveUserCases() {
  try {
    localStorage.setItem(USER_KEY, JSON.stringify(userCases.slice(0, 8)));
  } catch {
    /* private mode */
  }
}
function renderRail() {
  const sims = simulatedCases();
  const live = liveRows
    .map(
      (r) => `<button class="cat-item" data-live="${r.id}" style="--vc:${STATUS[r.status].css}"><span class="mini-coin" style="background-image:url(${coinImg(r.status)})"></span>
      <span class="vd">${STATUS[r.status].short}${r.synthetic ? " · synthetic" : ""}</span><span class="ttl">Cluster ${r.id}</span>
      <span class="meta">${r.size ?? "?"} accounts · score ${r.score.toFixed(2)}</span></button>`,
    )
    .join("");
  $("#rail").innerHTML = `
    ${userCases.length ? `<div class="ol-head">Your traces<span class="cnt">${userCases.length}</span></div>${userCases.map(railItem).join("")}` : ""}
    ${live ? `<div class="ol-head">Live from the monitor<span class="cnt">${liveRows.length}</span></div>${live}` : ""}
    <div class="ol-head">Case files<span class="cnt">${sims.length}</span></div>
    ${sims.map(railItem).join("")}
    <p class="rail-note"><b>Case files are simulated</b> (fictional accounts, <span class="mono" style="font-size:11px">.example</span> sites) except Dürer's Rhinoceros, which is real history. ${live ? "Live clusters come straight from the detector." : "Live clusters appear here when the monitor API is reachable."}</p>`;
  $("#rail")
    .querySelectorAll<HTMLButtonElement>("[data-case]")
    .forEach((b) => {
      const c = [...userCases, ...sims].find((x) => x.id === b.dataset.case)!;
      spark(c, b.querySelector("canvas")!);
    });
}
$("#rail").addEventListener("click", async (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-case],[data-live]");
  if (!b) return;
  app.classList.remove("rail-open");
  if (b.dataset.live) {
    try {
      openCase(await liveCase(Number(b.dataset.live)), "spread");
    } catch {
      toast("Couldn't load that cluster from the API");
    }
    return;
  }
  const c = [...userCases, ...simulatedCases()].find((x) => x.id === b.dataset.case);
  if (c) openCase(c, "spread");
});

// ---------------------------------------------------------------- view mode + actions
const seg = $("#mode");
seg.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-mode]");
  if (b) setMode(b.dataset.mode as Mode);
});
function setMode(m: Mode) {
  seg.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("on", (x as HTMLElement).dataset.mode === m));
  g.setMode(m);
  $(".ruler").style.opacity = m === "timeline" ? "1" : "0";
  setTimeout(() => g.fit(undefined, 1100), 380);
}
document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
  if (!a || a.closest("#panel") || a.closest(".event")) return;
  const act = a.dataset.act;
  if (act === "trace") openModal();
  if (act === "rail") app.classList.toggle("rail-open");
  if (act === "sheet") app.classList.toggle("sheet-open");
});
stage.querySelector(".zoom")!.addEventListener("click", (e) => {
  const z = (e.target as HTMLElement).closest<HTMLElement>("[data-z]")?.dataset.z;
  if (z === "in") g.zoomBy(1.35);
  if (z === "out") g.zoomBy(1 / 1.35);
  if (z === "fit") g.fit();
});

// ---------------------------------------------------------------- graph events
g.onSelect = (id) => {
  panel.showFolio(!!id);
  g.setHighlight(id ? g.lineage(id) : null);
  if (id && innerWidth < 860) app.classList.add("sheet-open");
};
g.onHover = (id) => {
  if (tracing) return;
  g.setHighlight(id ? g.lineage(id) : g.selId ? g.lineage(g.selId) : null);
};
g.onFrame = () => {
  dock.tick(performance.now());
  drawRuler(ruler, g);
  mini.draw();
  updateClock();
};
dock.onScrub = () => {
  if (tracing) cancelTrace();
};
panel.onReplay = () => current && runTrace(current);
setTimeout(() => ($("#hint").style.opacity = "0"), 7000);
stage.addEventListener("pointerdown", () => ($("#hint").style.opacity = "0"), { once: true });

// ---------------------------------------------------------------- opening a case
function openCase(c: TraceCase, how: "spread" | "static") {
  cancelTrace();
  closeEvent();
  current = c;
  renderRail();
  g.load(c, { revealAll: how === "static" });
  renderResources(c);
  panel.set(c, false);
  hideConsole();
  if (how === "spread") {
    dock.seek(0);
    g.fit([c.originId], 0, 0.9);
    setTimeout(() => {
      dock.play();
      const t0 = performance.now();
      const follow = () => {
        if (!dock.playing || current !== c) return;
        dock.u = Math.min(1, dock.u + 0.012);
        if (performance.now() - t0 < 4000) requestAnimationFrame(follow);
      };
      follow();
      setTimeout(() => current === c && g.followFit(4200), 1200);
    }, 350);
  } else {
    dock.u = 1;
    g.fit(undefined, 0);
    g.followFit(1500);
  }
}

// ---------------------------------------------------------------- trackdown choreography
function hideConsole() {
  consoleEl.classList.add("hidden");
}
function cancelTrace() {
  if (tracing) skipFlag = true;
}
function consoleHead(state: "run" | "done", c: TraceCase) {
  const mode = c.synthetic ? "simulated" : c.historical ? "archive" : "live";
  return `<div class="term-head"><span class="rec"></span><span>${state === "run" ? "Trackdown // tracking" : "Trackdown // complete"} · ${mode}</span><button class="x" data-skip title="Skip (Esc)">${state === "run" ? "Skip ›" : "Close"}</button></div><div class="term-body"><div class="cmd">trackdown --post ${esc(c.submittedId.slice(0, 12))}${state === "done" ? " --done" : ""}</div></div>`;
}
consoleEl.addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest("[data-skip]")) {
    if (tracing) skipFlag = true;
    else hideConsole();
  }
});

async function typeLine(body: HTMLElement, tool: keyof typeof TOOL, text: string, run: number) {
  const row = document.createElement("div");
  row.className = "line active";
  row.innerHTML = `<span class="gt">&gt;</span><span class="tool t-${tool}">${TOOL[tool].label}</span><span class="txt"></span><span class="res"></span>`;
  body.appendChild(row);
  body.scrollTop = body.scrollHeight;
  const t = row.querySelector<HTMLElement>(".txt")!;
  for (let i = 1; i <= text.length; i += 2) {
    if (skipFlag || run !== tracing) break;
    t.textContent = text.slice(0, i);
    await wait(11);
  }
  t.textContent = text;
  return row;
}

const CV = "#3fe07a";
async function runTrace(c: TraceCase) {
  const run = ++tracing;
  skipFlag = false;
  closeEvent();
  current = c;
  renderRail();
  g.load(c, { revealAll: false });
  renderResources(c);
  dock.pause();
  dock.u = 1;
  panel.set(c, true);
  consoleEl.innerHTML = consoleHead("run", c);
  consoleEl.classList.remove("hidden");
  const body = $(".term-body", consoleEl);
  const alive = () => run === tracing && !skipFlag;
  const pause = async (ms: number) => {
    if (alive()) await wait(ms);
  };
  const frontier: string[] = [];
  const reveal = (ids: string[], from?: string) => {
    g.reveal(ids, from);
    for (const id of ids) {
      const el = g.byId.get(id)?.el;
      if (el) {
        el.classList.remove("lock");
        void el.offsetWidth;
        el.classList.add("lock");
      }
    }
  };
  const subPath = g.pathToOrigin(c.submittedId);

  for (const step of c.steps) {
    if (!alive()) break;
    const row = await typeLine(body, step.tool, step.text, run);
    const found = step.found ?? [];
    if (step.tool === "you") {
      reveal(found);
      g.focus(c.submittedId, 1.05, 0);
      const el = g.byId.get(c.submittedId)?.el;
      el?.classList.add("scanning");
      g.ring(c.submittedId, CV, 260, 1500);
      await pause(1400);
      el?.classList.remove("scanning");
      frontier.push(c.submittedId);
    } else if (found.length) {
      const ids = new Set<string>();
      for (const f of found) {
        ids.add(f);
        const i = subPath.indexOf(f);
        if (i > 0) subPath.slice(0, i).forEach((x) => ids.add(x));
        const node = c.nodes.find((n) => n.id === f);
        if (node?.cluster) c.clusters.find((cl) => cl.id === node.cluster)?.members.forEach((m) => ids.add(m));
      }
      const newIds = [...ids].filter((id) => !g.revealed?.has(id));
      const anchor = frontier[frontier.length - 1];
      if (step.rejected && anchor) g.ghost(anchor, Math.min(step.rejected, 6));
      await pause(step.rejected ? 700 : 200);
      const chunk = newIds.length > 8 ? 4 : 1;
      for (let i = 0; i < newIds.length; i += chunk) {
        if (!alive()) break;
        reveal(newIds.slice(i, i + chunk), found.includes(newIds[i]) ? anchor : undefined);
        if (newIds.length > 1) await wait(newIds.length > 8 ? 45 : 180);
      }
      if (!alive()) break;
      for (const f of found) {
        g.ring(f, ROLE[c.nodes.find((n) => n.id === f)!.role].color, 300, 1500);
        const el = g.byId.get(f)?.el;
        if (el?.querySelector(".cvbox")) {
          el.classList.add("scanning");
          setTimeout(() => el.classList.remove("scanning"), 1200);
        }
      }
      frontier.push(...found);
      await wait(120);
      g.fit([...(g.revealed ?? [])], 1100, 1);
      row.querySelector(".res")!.innerHTML = `<b>+${newIds.length}</b>${step.rejected ? ` −${step.rejected}` : ""}`;
      await pause(1300);
    } else {
      const id = frontier[frontier.length - 1];
      const el = id ? g.byId.get(id)?.el : null;
      el?.classList.add("scanning");
      await pause(1100);
      el?.classList.remove("scanning");
    }
    row.classList.remove("active");
  }
  if (run !== tracing) return;

  if (alive() && c.originId) {
    g.setHighlight(new Set(g.pathToOrigin(c.submittedId)));
    g.ring(c.originId, ROLE.origin.color, 520, 2200);
    await typeLine(body, c.historical ? "archive" : "detector", `earliest known copy: ${fmtShort(c.nodes.find((n) => n.id === c.originId)!.t)} · ${Math.max(0, subPath.length - 1)} copies back`, run);
    await pause(1500);
    g.setHighlight(null);
  }
  if (alive()) {
    const rest = c.nodes.filter((n) => !g.revealed?.has(n.id)).sort((a, b) => a.t - b.t);
    if (rest.length) {
      await typeLine(body, c.historical ? "archive" : "detector", `mapping the rest of the spread: ${rest.length} more`, run);
      g.fit(undefined, 1400);
      for (let i = 0; i < rest.length; i += 3) {
        if (!alive()) break;
        reveal(rest.slice(i, i + 3).map((n) => n.id));
        await wait(55);
      }
    }
  }
  g.revealAll();
  dock.u = 1;
  consoleEl.innerHTML = consoleHead("done", c);
  const b2 = $(".term-body", consoleEl);
  const hops = Math.max(0, subPath.length - 1);
  b2.insertAdjacentHTML("beforeend", `<div class="line"><span class="gt">&gt;</span><span class="tool t-${c.historical ? "archive" : "gemini"}">verdict</span><span class="txt">${STATUS[c.status].word.toLowerCase()} · ${hops} copies back · ${c.nodes.length} posts</span><span class="res"></span></div>`);
  g.followFit(3200);
  const wasSkipped = skipFlag;
  skipFlag = false;
  await wait(wasSkipped ? 300 : 800);
  if (run !== tracing) return;
  panel.set(c, false);
  if (!wasSkipped) showEvent(c, run);
  if (run === tracing) {
    tracing = 0;
    setTimeout(() => !tracing && hideConsole(), 6000);
  }
}

// ---------------------------------------------------------------- EU4-style event window for the verdict
let eventEl: HTMLElement | null = null;
function closeEvent() {
  if (!eventEl) return;
  const e = eventEl;
  eventEl = null;
  e.classList.add("out");
  setTimeout(() => e.remove(), 280);
}
async function showEvent(c: TraceCase, run: number) {
  closeEvent();
  const v = STATUS[c.status];
  const hops = Math.max(0, g.pathToOrigin(c.submittedId).length - 1);
  const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
  const scored = c.scored !== false && c.status !== "insufficient";
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="event gilt hud-corners" role="dialog" aria-label="Verdict">
    <div class="banner"><canvas></canvas><div class="coin-slot"></div><div class="verdict-word" style="color:${v.css}">${v.word}</div><span class="credit">${c.historical ? "Dürer, Rhinocerus, 1515" : "Botticelli, c. 1485 · ORB keypoints"}</span></div>
    <div class="ev-body">
      <h2 class="ev-title">${esc(c.title)}</h2>
      <div class="ev-sub">Attribution · ${esc(c.kicker)}${c.synthetic ? " · simulated" : c.historical ? " · historical record" : ""}</div>
      <p class="ev-text">${esc(c.summary)}</p>
      <div class="effects">
        <div class="row"><span>Verdict</span><b style="color:${v.css}">${v.word} · ${v.tag}</b></div>
        <div class="row"><span>Confidence</span><b>${scored ? Math.round(c.confidence * 100) + "%" : c.status === "insufficient" ? "Abstained" : "Not scored"}</b></div>
        <div class="row"><span>Copies back to the earliest</span><b>${hops}</b></div>
        <div class="row"><span>${c.historical ? "Hands involved" : "Accounts involved"}</span><b>${accounts}</b></div>
      </div>
      <div class="opts">
        ${c.clusters.length ? `<button class="btn" data-ev="territory">Inspect the ${esc(c.clusters[0].label.toLowerCase())}</button>` : ""}
        <button class="btn" data-ev="origin">Go to the earliest copy</button>
        <button class="btn" data-ev="receipts">Read the receipts</button>
        <button class="btn gold" data-ev="close">Noted</button>
      </div>
    </div></div>`;
  document.body.appendChild(back);
  eventEl = back;
  const cvs = $<HTMLCanvasElement>("canvas", back);
  let src: HTMLImageElement | undefined;
  if (c.historical) {
    src = new Image();
    src.src = rhinoUrl;
  }
  mountBanner(cvs, { darken: c.historical ? 0.55 : 0.4, focusX: 0.55, per: 70, src });
  back.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const act = t.closest<HTMLElement>("[data-ev]")?.dataset.ev;
    if (t === back || act === "close") closeEvent();
    if (act === "territory") {
      closeEvent();
      g.fit(c.clusters[0].members, 900);
    }
    if (act === "origin") {
      closeEvent();
      g.select(c.originId);
      g.focus(c.originId, 1, 900);
    }
    if (act === "receipts") {
      closeEvent();
      panel.tab = "receipts";
      panel.render();
      app.classList.add("sheet-open");
    }
  });
  await bannerReady;
  await wait(250);
  await flipInto($(".coin-slot", back), c.status, () => run !== tracing && tracing !== 0);
  $(".verdict-word", back).animate([{ opacity: 0, letterSpacing: "0.8em" }, { opacity: 1, letterSpacing: "0.3em" }], { duration: 700, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
}

// ---------------------------------------------------------------- trace modal
const EXAMPLES = [
  "They're banning tap water on weekends. NOT a joke. Share before it gets deleted",
  "$GLOW partnership with a top payments network confirmed 🚀",
  "This flood photo is happening RIGHT NOW and the media is silent",
  "Harbour bridge closed both ways, loud bang from the east pillar",
];
function openModal() {
  if (document.querySelector(".modal")) return;
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal gilt hud-corners" role="dialog" aria-label="Trace a post">
    <div class="banner"><canvas></canvas><div class="bt">SHILL OR CHILL</div><span class="credit">Botticelli, c. 1485 · live ORB keypoints</span></div>
    <div class="form">
      <p class="sub">Drop a post, a link or a handle. We walk it back to the earliest copy we can find, then look at how it spread.</p>
      <div class="drop"><textarea placeholder="Paste a post, a bsky.app link or @handle…"></textarea>
        <div class="row">◈ Drop a screenshot · Nemotron reads it<span class="file"></span></div></div>
      <div class="examples">${EXAMPLES.map((e) => `<button data-ex="${esc(e)}">${esc(e.length > 54 ? e.slice(0, 52) + "…" : e)}</button>`).join("")}</div>
      <div class="foot"><div class="fine">Asks the live API first. If it isn't reachable, you get a clearly labelled simulated trackdown.</div><button class="btn" data-x>Cancel</button><button class="btn gold" data-go>${sm(icon.search, 14)} Trace it</button></div>
    </div>
  </div>`;
  document.body.appendChild(back);
  mountBanner($<HTMLCanvasElement>("canvas", back), { darken: 0.42, per: 80 });
  const ta = $<HTMLTextAreaElement>("textarea", back);
  const drop = $(".drop", back);
  let file: File | null = null;
  setTimeout(() => ta.focus(), 60);
  const close = () => {
    back.classList.add("out");
    setTimeout(() => back.remove(), 280);
  };
  back.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    if (t === back || t.closest("[data-x]")) close();
    const ex = t.closest<HTMLElement>("[data-ex]");
    if (ex) {
      ta.value = ex.dataset.ex!;
      ta.focus();
    }
    if (t.closest("[data-go]")) go();
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) go();
    if (e.key === "Escape") close();
  });
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    const f = e.dataTransfer?.files?.[0];
    if (f) {
      file = f;
      $(".file", drop).innerHTML = `<span class="att">${esc(f.name.slice(0, 28))}</span>`;
    } else {
      const txt = e.dataTransfer?.getData("text");
      if (txt) ta.value = txt;
    }
  });
  async function go() {
    const text = ta.value.trim() || (file ? "Screenshot" : "");
    if (!text) {
      ta.focus();
      drop.animate([{ transform: "translateX(0)" }, { transform: "translateX(-6px)" }, { transform: "translateX(6px)" }, { transform: "translateX(0)" }], { duration: 300 });
      return;
    }
    $<HTMLButtonElement>("[data-go]", back).textContent = "Asking the API…";
    let c = await analyze(text);
    if (!c) {
      c = simulateFromInput(text);
      toast("Live API not reachable · running a simulated trackdown");
    }
    userCases.unshift(c);
    saveUserCases();
    close();
    runTrace(c);
  }
}

let toastT = 0;
function toast(msg: string) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast hud-corners";
  t.textContent = msg;
  document.body.appendChild(t);
  clearTimeout(toastT);
  toastT = window.setTimeout(() => t.remove(), 3400);
}

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  if ((e.target as HTMLElement).closest("textarea,input")) return;
  if (e.key === "Escape") {
    if (eventEl) closeEvent();
    else if (tracing) skipFlag = true;
    else if (g.selId) g.select(null);
    else hideConsole();
  }
  if (e.key === " ") {
    e.preventDefault();
    dock.toggle();
  }
  if (e.key === "f" || e.key === "F") g.fit();
  if (e.key === "t" || e.key === "T") setMode(g.mode === "timeline" ? "web" : "timeline");
  if (e.key === "n" || e.key === "N") {
    e.preventDefault();
    openModal();
  }
  if (e.key === "+" || e.key === "=") setSpeed(speedIx + 1);
  if (e.key === "-") setSpeed(speedIx - 1);
  if (/^[1-9]$/.test(e.key)) {
    const all = [...userCases, ...simulatedCases()];
    const c = all[Number(e.key) - 1];
    if (c) openCase(c, "spread");
  }
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowDown") && current) {
    e.preventDefault();
    const sel = g.selId ? g.byId.get(g.selId) : g.byId.get(current.submittedId);
    if (!sel) return;
    let next = sel;
    if (e.key === "ArrowLeft") next = sel.parents[0] ?? sel;
    if (e.key === "ArrowRight") next = sel.children.filter((c) => c.vis).sort((a, b) => a.d.t - b.d.t)[0] ?? sel;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const sib = (sel.parents[0]?.children ?? [sel]).filter((c) => c.vis).sort((a, b) => a.y - b.y);
      const i = sib.indexOf(sel);
      next = sib[Math.max(0, Math.min(sib.length - 1, i + (e.key === "ArrowUp" ? -1 : 1)))] ?? sel;
    }
    g.select(next.d.id);
    g.focus(next.d.id, Math.max(g.k, 0.9), 500);
  }
});

// ---------------------------------------------------------------- boot
renderRail();
const params = new URLSearchParams(location.search);
const first = simulatedCases().find((c) => c.id === params.get("case")) ?? simulatedCases()[0];
if (params.has("static")) openCase(first, "static");
else {
  // title card: the painting is read by machine vision, then the trackdown starts
  const splash = document.createElement("div");
  splash.className = "splash";
  splash.innerHTML = `<canvas></canvas><div class="t"><div class="big">SHILL OR CHILL</div><div class="sub">Provenance for the feed · Junction 2026</div></div>`;
  document.body.appendChild(splash);
  mountBanner($<HTMLCanvasElement>("canvas", splash), { darken: 0.45, per: 75, delay: 200 });
  let gone = false;
  const go = () => {
    if (gone) return;
    gone = true;
    splash.classList.add("out");
    setTimeout(() => splash.remove(), 850);
    runTrace(first);
  };
  splash.addEventListener("click", go);
  Promise.all([document.fonts.ready, bannerReady, wait(2600)]).then(go);
}
listLive().then((rows) => {
  liveRows = rows;
  if (rows.length) renderRail();
});
