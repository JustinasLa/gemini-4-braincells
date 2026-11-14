import "./styles.css";
import rhinoUrl from "./assets/rhino-1515.webp";
import fullCv from "./assets/venus-full-cv.json";
import venusFullUrl from "./assets/venus-full.webp";
import { analyze, listLive, liveCase, type ClusterSummary } from "./data/api";
import { simulatedCases } from "./data/cases";
import { LookupError, lookupCase, startLookup, type LookupResult } from "./data/lookup";
import { simulateFromInput } from "./data/simulate";
import { Graph, type Mode } from "./engine/graph";
import type { TraceCase } from "./types";
import { flipInto, preloadCoin, wait } from "./ui/coin";
import { thumbStyle } from "./ui/crop";
import { bannerReady, mountBanner } from "./ui/cvbanner";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtDate, fmtDur, fmtShort, ROLE, STATUS, TOOL } from "./ui/format";
import { icon } from "./ui/icons";
import { hasChain, Panel } from "./ui/panel";
import { initTooltips } from "./ui/tooltip";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;
const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <a class="wordmark" href="/monitor/" title="Back to the live monitor" aria-label="Shill or Chill: back to the live monitor">SHILL OR CHILL</a>
    <div class="case-head"><button class="btn ghost" data-act="rail" aria-label="Cases" aria-expanded="false" aria-controls="rail">${sm(icon.menu, 14)}<span class="lbl">Cases</span></button><h1 id="title"></h1><span id="badges"></span></div>
    <div class="top-actions">
      <div class="seg" id="mode" role="group" aria-label="Layout"><button data-mode="timeline" class="on" aria-pressed="true">Timeline</button><button data-mode="web" aria-pressed="false">Web</button></div>
      <button class="btn solid" data-act="trace" aria-label="New trace">${icon.search}<span class="lbl">New trace</span></button>
    </div>
  </header>
  <aside class="rail"><div class="rail-inner" id="rail"></div></aside>
  <main class="stage" id="stage" tabindex="0" role="application" aria-roledescription="spread map" aria-label="Spread map. Arrow keys move between posts: left to the earlier copy, right to a later one, up and down between siblings.">
    <div class="sr-only" aria-live="polite" id="announce"></div>
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="term hidden" id="console" data-ui></div>
    <div class="status" id="status" data-ui inert><span class="st-tool"></span><span class="st-txt"></span><button class="st-skip label" data-skip>skip</button></div>
    <div class="legend" id="legend" data-ui></div>
    <div class="hud" data-ui>
      <div class="zoom"><button data-z="in" aria-label="Zoom in">${icon.plus}</button><button data-z="out" aria-label="Zoom out">${icon.minus}</button><button data-z="fit" aria-label="Fit">${icon.fit}</button><button data-z="time" class="mode-btn on" aria-pressed="true" aria-label="Timeline layout">TIME</button><button data-z="web" class="mode-btn" aria-pressed="false" aria-label="Web layout">WEB</button><button data-z="tools" aria-label="Key and map" aria-expanded="false" title="Key and map (M)">KEY</button></div>
      <div class="minimap" id="minimap"><span class="mm-title label">map</span></div>
    </div>
    <div class="dock" id="dock" data-ui></div>
    <div class="empty-stage" id="empty" data-ui hidden><h2>No chain of posts to draw</h2><p>The detector grouped this live cluster by how its accounts behaved, not by copied posts we can lay out. The verdict and the evidence are in the panel.</p></div>
    <button class="peek" id="peek" data-ui data-act="sheet" aria-label="Show verdict and evidence"></button>
    <div class="vignette"></div>
  </main>
  <aside class="panel" id="panel"></aside>
</div>`;

const app = $(".app");
// Closed drawers must not take keyboard focus; keep inert + aria-expanded in sync with the classes.
const phoneMq = matchMedia("(max-width: 860px)"); // the CSS breakpoint
const syncDrawers = () => {
  const phone = phoneMq.matches;
  const rail = $(".rail");
  const pnl = $("#panel");
  rail.inert = !app.classList.contains("rail-open");
  const sheet = phone && app.classList.contains("sheet-open");
  pnl.inert = phone ? !sheet : !(app.classList.contains("panel-open") || app.classList.contains("lookup-active"));
  if (sheet) {
    pnl.setAttribute("role", "dialog");
    pnl.setAttribute("aria-modal", "true");
    pnl.setAttribute("aria-label", "Verdict and evidence");
  } else ["role", "aria-modal", "aria-label"].forEach((a) => pnl.removeAttribute(a));
  // an open sheet covers the page: nothing behind it may take focus
  $(".topbar").inert = sheet;
  stage.inert = sheet;
  if (sheet) rail.inert = true;
  document.querySelector('[data-act="rail"]')?.setAttribute("aria-expanded", String(app.classList.contains("rail-open")));
  document.querySelector("#peek")?.setAttribute("aria-expanded", String(app.classList.contains("sheet-open")));
};
new MutationObserver(syncDrawers).observe(app, { attributes: true, attributeFilter: ["class"] });
phoneMq.addEventListener("change", () => {
  if (!phoneMq.matches) app.classList.remove("sheet-open");
  syncDrawers();
  setInsets();
  if (current) setTimeout(() => (g.selId ? g.focus(g.selId, Math.max(g.k, 0.9), 600) : g.fit(undefined, 600)), 500);
});
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
let traceSequence = 0;
let lookupController: AbortController | null = null;
let skipFlag = false;
let liveRows: ClusterSummary[] = [];
preloadCoin();
initTooltips();

// ---------------------------------------------------------------- legend
$("#legend").innerHTML =
  `<div class="legend-title"><span class="label">Key</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="sw member" style="--c:${ROLE.amplifier.color}"></span>Cluster member · zoom in to read</div>` +
  `<div class="li"><span class="ln" style="--s:dashed;--c:#f5f5f5"></span>Match (copy, image)</div><div class="li"><span class="ln" style="--c:#b4b0a8"></span>Repost, quote</div><div class="li"><span class="ln" style="--s:dashed;--c:#7aa2f0"></span>Cites</div><div class="legend-hint">Select a marker to read the post.</div>`;


// ---------------------------------------------------------------- phases: home -> trace -> done
// Only what the moment needs is on screen: the canvas + one status line while tracing,
// the verdict panel and timeline once there is a verdict. Cases, map and key are opt-in.
type Phase = "home" | "trace" | "done";
function setPhase(p: Phase) {
  app.dataset.phase = p;
  app.classList.toggle("panel-open", p === "done");
  if (p !== "done") app.classList.remove("rail-open");
  setInsets();
  if (p === "done") setTimeout(() => {
    setInsets(); // the dock has finished moving in by now
    g.fit(undefined, 900);
  }, 650);
}

/** camera insets follow the layout: phones stack the verdict bar and a two-row dock at the bottom */
function setInsets() {
  const phone = phoneMq.matches;
  // frame above whatever sits at the bottom of the stage (dock, phone verdict bar), measured
  const dock = $("#dock").getBoundingClientRect();
  const st = stage.getBoundingClientRect();
  g.insets.bottom = app.dataset.phase === "done" && dock.height ? Math.max(64, st.bottom - dock.top + 12) : 64;
  g.insets.right = phone ? 64 : 72; // the control column sits on the right
  g.insets.left = phone ? 12 : 24;
}

// ---------------------------------------------------------------- header
function renderResources(c: TraceCase) {
  const v = STATUS[c.status];
  $("#title").textContent = c.title;
  const tag = c.mock ? '<span class="tag sim">MOCK · fictional data</span>' : c.synthetic ? '<span class="tag sim">simulated</span>' : c.historical ? '<span class="tag hist">historical record</span>' : "";
  $("#badges").innerHTML = `<span style="display:flex;gap:10px;align-items:center"><span class="vchip" style="--vc:${v.css}">${v.short.toUpperCase()}</span>${tag}</span>`;
  const origin = c.synthetic ? "Simulated" : c.historical ? "Historical record" : c.live ? "Live" : "";
  const peek = $("#peek");
  peek.style.setProperty("--vc", v.css);
  peek.innerHTML = `<span class="vchip" style="--vc:${v.css}">${v.short.toUpperCase()}</span><span class="pk-t"><b>${esc(c.title)}</b><span>${origin}</span></span><span class="pk-go">Details ↑</span>`;
  const empty = !hasChain(c) && !c.findingsOnly && !c.lookup; // a link lookup draws what it collected
  if (empty) app.dataset.empty = "";
  else delete app.dataset.empty;
  // search-only results have no known dates: no time axis, no playback
  if (c.findingsOnly) app.dataset.notime = "";
  else delete app.dataset.notime;
  $("#empty").hidden = !empty;
  document.title = `${c.title} · Trackdown · Shill or Chill`;
}

// ---------------------------------------------------------------- outliner
function railItem(c: TraceCase) {
  const sub = c.nodes.find((n) => n.id === c.submittedId);
  const ori = c.nodes.find((n) => n.id === c.originId);
  const th = c.historical ? { style: `background-image:url(${rhinoUrl});background-position:72% 40%;background-size:260%`, score: "1515" } : thumbStyle(c.id, 64);
  return `<button class="cat-item${current?.id === c.id ? " on" : ""}" data-case="${c.id}" style="--vc:${STATUS[c.status].css}">
    <span class="thumb" style="${th.style}" data-score="${th.score}"></span>
    <span class="vd">${STATUS[c.status].short} · ${esc(SHORT_KICKER[c.kicker] ?? c.kicker)}</span>
    <span class="ttl">${esc(c.title)}</span>
    <span class="meta">${railSpan(ori?.t, sub?.t)}</span>
  </button>`;
}

/** one short line: years for long spans, dates for days, one date for a single day */
function railSpan(a?: number, b?: number) {
  if (a == null || b == null) return "";
  const span = b - a;
  const yr = (t: number) => String(new Date(t).getUTCFullYear());
  if (span > 365 * 86400000) return `${yr(a)} → ${yr(b)} · ${fmtDur(span)}`;
  if (span > 86400000) return `${fmtShort(a)} → ${fmtShort(b)}`;
  return `${fmtShort(a)} · ${fmtDur(span)}`;
}

// rail captions are one line in a 300px rail: short forms of the case kickers
const SHORT_KICKER: Record<string, string> = { "Historical record": "History", "Breaking news burst": "Breaking news", "Out-of-context photo": "Old photo", "Recycled satire": "Satire", "Token pump": "Token pump", "Your input": "Your input", "Live cluster": "Live" };

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
      (r) => `<button class="cat-item live${current?.id === `live-${r.id}` ? " on" : ""}" data-live="${r.id}" style="--vc:${STATUS[r.status].css}">
      <span class="vd">${STATUS[r.status].short}${r.synthetic ? " · synthetic" : ""}</span><span class="ttl">#${r.id}</span>
      <span class="meta">${r.size ?? "?"} accounts</span></button>`,
    )
    .join("");
  $("#rail").innerHTML = `
    ${userCases.length ? `<div class="label"><span>your traces</span><span>${userCases.length}</span></div>${userCases.map(railItem).join("")}` : ""}
    <div class="label"><span>case files</span><span>${sims.length}</span></div>
    ${sims.map(railItem).join("")}
    ${live ? `<div class="label"><span>live from the monitor</span><span>${liveRows.length}</span></div>${live}` : ""}
    <p class="rail-note"><b>Case files are simulated</b> (fictional accounts, <span class="mono" style="font-size:11px">.example</span> sites) except Dürer's Rhinoceros, which is real history. ${live ? "Live clusters come straight from the detector." : "Live clusters appear here when the monitor API is reachable."}</p>`;
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
  stage.querySelectorAll<HTMLElement>(".mode-btn").forEach((b) => {
    const on = (b.dataset.z === "time") === (m === "timeline");
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  seg.querySelectorAll<HTMLElement>("[data-mode]").forEach((x) => {
    x.classList.toggle("on", x.dataset.mode === m);
    x.setAttribute("aria-pressed", String(x.dataset.mode === m));
  });
  g.setMode(m);
  $(".ruler").style.opacity = m === "timeline" ? "1" : "0";
  setTimeout(() => g.fit(undefined, 1100), 380);
}
document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
  if (!a || a.closest("#panel") || a.closest(".event")) return;
  const act = a.dataset.act;
  if (act === "trace") showHome();
  if (act === "rail") {
    app.classList.toggle("rail-open");
    if (app.classList.contains("rail-open")) setTimeout(() => $("#rail").querySelector<HTMLElement>("button")?.focus(), 50);
    setTimeout(() => g.fit(undefined, 800), 650);
  }
  if (act === "sheet") {
    app.classList.remove("rail-open");
    app.classList.toggle("sheet-open");
    if (app.classList.contains("sheet-open")) setTimeout(() => $("#panel").querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus(), 50);
  }
});
stage.querySelector(".zoom")!.addEventListener("click", (e) => {
  const z = (e.target as HTMLElement).closest<HTMLElement>("[data-z]")?.dataset.z;
  if (z === "in") g.zoomBy(1.35);
  if (z === "out") g.zoomBy(1 / 1.35);
  if (z === "fit") g.fit();
  if (z === "tools") app.classList.toggle("tools");
  if (z === "time") setMode("timeline");
  if (z === "web") setMode("web");
  stage.querySelector('[data-z="tools"]')!.setAttribute("aria-expanded", String(app.classList.contains("tools")));
});

// ---------------------------------------------------------------- graph events
g.onSelect = (id) => {
  panel.showFolio(!!id);
  g.setHighlight(id ? g.lineage(id) : null);
  if (id && phoneMq.matches) {
    const fromKeyboard = document.activeElement === stage;
    app.classList.add("sheet-open");
    syncDrawers();
    // the sheet is modal: focus follows into it
    if (fromKeyboard) $("#panel").querySelector<HTMLElement>('[data-tab="folio"]')?.focus();
  }
  const n = id ? current?.nodes.find((x) => x.id === id) : null;
  $("#announce").textContent = n ? `${n.author.name}, ${ROLE[n.role].label}${n.undated || n.timeUnknown ? "" : `, ${fmtDate(n.t)}`}` : "";
};
g.onHover = (id) => {
  if (tracing) return;
  g.setHighlight(id ? g.lineage(id) : g.selId ? g.lineage(g.selId) : null);
};
g.onFrame = () => {
  dock.tick(performance.now());
  drawRuler(ruler, g);
  mini.draw();
};
dock.onScrub = () => {
  if (tracing) cancelTrace();
};
panel.onReplay = () => current && (current.lookup ? openCase(current, "static") : runTrace(current));
panel.onCloseSheet = () => {
  app.classList.remove("sheet-open");
  syncDrawers(); // un-inert the page now, so focus can land on Details
  $("#peek").focus();
};

// ---------------------------------------------------------------- opening a case
function openCase(c: TraceCase, how: "spread" | "static") {
  cancelTrace();
  closeEvent();
  hideHome();
  setPhase("done");
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
  if (tracing) { skipFlag = true; tracing = 0; }
  lookupController?.abort();
  lookupController = null;
  consoleEl.classList.remove("lookup-progress");
  app.classList.remove("lookup-active");
  setStatus(null, "");
}
function consoleHead(state: "run" | "done", c: TraceCase) {
  const mode = c.synthetic ? "--simulated" : c.historical ? "--archive" : "";
  return `<div class="term-head"><span class="d r"></span><span class="d y"></span><span class="d g"></span><span class="file">trackdown.sh ${mode}</span><button class="x" data-skip title="Skip (Esc)">${state === "run" ? "skip" : "close"}</button></div><div class="term-body"><div class="cmd">trackdown --post ${esc(c.submittedId.slice(0, 12))}${state === "done" ? " --done" : ""}</div></div>`;
}
const statusEl = $("#status");
statusEl.addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest("[data-skip]") && !lookupController) skipFlag = true;
});
consoleEl.addEventListener("click", (event) => {
  if ((event.target as HTMLElement).closest("[data-close-console]")) hideConsole();
});
function setStatus(tool: keyof typeof TOOL | null, text: string) {
  statusEl.classList.toggle("on", !!text);
  statusEl.inert = !text; // a hidden status line must not hold a focus stop
  const tl = $(".st-tool", statusEl);
  tl.className = `st-tool t-${tool ?? "detector"}`;
  tl.textContent = tool ? TOOL[tool].label : "";
  $(".st-txt", statusEl).textContent = text;
}

async function typeLine(body: HTMLElement, tool: keyof typeof TOOL, text: string, run: number) {
  const row = document.createElement("div");
  row.className = "line active";
  row.innerHTML = `<span class="gt">&gt;</span><span class="tool t-${tool}">${TOOL[tool].label}</span><span class="txt"></span><span class="res"></span>`;
  body.appendChild(row);
  const t = row.querySelector<HTMLElement>(".txt")!;
  for (let i = 1; i <= text.length; i += 2) {
    if (skipFlag || run !== tracing) break;
    t.textContent = text.slice(0, i);
    setStatus(tool, text.slice(0, i));
    await wait(11);
  }
  t.textContent = text;
  setStatus(tool, text);
  return row;
}

const CV = "#3fe07a";
async function runTrace(c: TraceCase) {
  cancelTrace();
  const run = tracing = ++traceSequence;
  skipFlag = false;
  closeEvent();
  hideHome();
  setPhase("trace");
  current = c;
  renderRail();
  g.load(c, { revealAll: false });
  renderResources(c);
  dock.pause();
  dock.u = 1;
  panel.set(c, true);
  consoleEl.innerHTML = consoleHead("run", c);
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
  setStatus(null, "");
  if (!wasSkipped) {
    closeEvent();
    afterReveal = () => setPhase("done");
    showEvent(c, run);
  } else setPhase("done");
  if (run === tracing) tracing = 0;
}

// ---------------------------------------------------------------- verdict reveal: the Junction banner with the verdict as its wordmark
let eventEl: HTMLElement | null = null;
let afterReveal: (() => void) | null = null;
function closeEvent() {
  const cb = afterReveal;
  afterReveal = null;
  if (cb) setTimeout(cb, 150);
  if (!eventEl) return;
  const e = eventEl;
  eventEl = null;
  e.classList.add("out");
  setTimeout(() => e.remove(), 480);
}
async function showEvent(c: TraceCase, run: number) {
  const v = STATUS[c.status];
  const el = document.createElement("div");
  el.className = "reveal";
  el.style.setProperty("--vc", v.css);
  el.innerHTML = `<canvas></canvas><div class="center"><div class="coin-slot"></div><div class="vw${v.word.length > 8 ? " long" : ""}">${v.word.toUpperCase()}</div><div class="vt">${v.tag}${c.synthetic ? " · simulated case" : c.historical ? " · historical record" : ""}</div></div><span class="tap label">click to continue</span><span class="credit">${c.historical ? "Dürer, Rhinocerus, 1515" : "Botticelli, The Birth of Venus, c. 1485 · ORB keypoints"}</span>`;
  stage.appendChild(el);
  eventEl = el;
  let src: HTMLImageElement | undefined;
  if (c.historical) {
    src = new Image();
    src.src = rhinoUrl;
  }
  mountBanner($<HTMLCanvasElement>("canvas", el), { darken: c.historical ? 0.5 : 0.42, per: 70, src, focusX: 0.5, avoid: () => [...el.querySelectorAll(".coin-slot, .vw, .vt, .tap")] });
  el.addEventListener("click", closeEvent);
  await bannerReady;
  await wait(300);
  await flipInto($(".coin-slot", el), c.status, () => run !== tracing && tracing !== 0);
  $(".vw", el).animate([{ opacity: 0, letterSpacing: "0.7em" }, { opacity: 1, letterSpacing: "0.3em" }], { duration: 800, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
  $(".vt", el).animate([{ opacity: 0 }, { opacity: 1 }], { duration: 600, delay: 350, fill: "forwards" });
  setTimeout(() => eventEl === el && closeEvent(), 5200);
}

// ---------------------------------------------------------------- home: the banner, one input, a few cases
let homeEl: HTMLElement | null = null;
let stopHomeBanner: (() => void) | null = null;
function showHome() {
  cancelTrace();
  closeEvent();
  if (homeEl) return;
  const cases = simulatedCases();
  const sims = cases.filter((c) => !c.historical);
  const hist = cases.filter((c) => c.historical);
  const caseBtn = (c: TraceCase) => `<button data-hcase="${c.id}" style="--vc:${STATUS[c.status].css}">${esc(c.title)}${c.historical ? ' <em>historical record</em>' : ""}</button>`;
  homeEl = document.createElement("div");
  homeEl.className = "home";
  homeEl.innerHTML = `<canvas></canvas>
    <a class="home-back" href="/monitor/">← Live monitor</a>
    <div class="home-c">
      <div class="home-wm">SHILL OR CHILL</div>
      <p class="home-tag">Paste a post. We trace it back to the earliest copy we can find and check whether its spread was coordinated.</p>
      <form class="home-form" aria-describedby="home-note"><input name="q" aria-label="Post text, link or handle" placeholder="Paste a post, a link or an @handle" autocomplete="off" /><button class="btn solid" type="submit">Trace it</button></form>
      <div class="home-ex"><span class="label">Or explore an example</span>${[...sims, ...hist].map(caseBtn).join("")}<p class="home-note" id="home-note">Examples use fictional accounts, except the historical record.</p></div>
    </div>
    <span class="credit">Botticelli, The Birth of Venus, c. 1485 · ORB keypoints</span>`;
  document.body.appendChild(homeEl);
  const img = new Image();
  img.src = venusFullUrl;
  const home = homeEl;
  stopHomeBanner = mountBanner($<HTMLCanvasElement>("canvas", homeEl), { darken: 0.4, per: 90, delay: 300, src: img, cv: fullCv as never, focusX: 0.5, focusY: 0.25, avoid: () => [...home.querySelectorAll(".home-back, .home-wm, .home-tag, .home-form, .home-ex, .credit")] });
  const input = $<HTMLInputElement>("input", homeEl);
  setTimeout(() => input.focus(), 400);
  $("form", homeEl).addEventListener("submit", (e) => {
    e.preventDefault();
    startTrace(input.value);
  });
  homeEl.addEventListener("click", (e) => {
    const b = (e.target as HTMLElement).closest<HTMLElement>("[data-hcase]");
    if (!b) return;
    const c = simulatedCases().find((x) => x.id === b.dataset.hcase);
    if (c) runTrace(c);
  });
}
function hideHome() {
  if (!homeEl) return;
  const el = homeEl;
  homeEl = null;
  el.classList.add("out");
  setTimeout(() => {
    stopHomeBanner?.();
    el.remove();
  }, 700);
}
async function startTrace(raw: string, mock = false) {
  const text = raw.trim();
  if (!text) {
    homeEl?.querySelector(".home-form")?.animate([{ transform: "translateX(0)" }, { transform: "translateX(-6px)" }, { transform: "translateX(6px)" }, { transform: "translateX(0)" }], { duration: 300 });
    return;
  }
  cancelTrace();
  closeEvent();
  hideHome();
  const run = tracing = ++traceSequence;
  const controller = new AbortController();
  lookupController = controller;
  skipFlag = false;
  setPhase("trace");
  app.classList.add("lookup-active");
  consoleEl.classList.add("lookup-progress");
  consoleEl.classList.remove("hidden");
  dock.pause();
  dock.u = 1;
  let graphSignature = "";
  // Each poll re-renders the console; only steps not shown before slide in, so it does not flash.
  const shownSteps = new Set<string>();
  const latest: { result: LookupResult | null } = { result: null };
  const update = (result: LookupResult) => {
    if (controller.signal.aborted || run !== tracing) return;
    latest.result = result;
    const trace = lookupCase(result);
    trace.mock = mock;
    if (mock) { trace.live = false; trace.synthetic = true; trace.kicker = "Development mock"; }
    current = trace;
    const signature = JSON.stringify([trace.nodes, trace.edges, trace.clusters]);
    if (signature !== graphSignature) {
      const selected = g.selId;
      const prior = new Map(g.nodes.map((node) => [node.d.id, { x: node.x, y: node.y }]));
      g.load(trace, { revealAll: true });
      for (const node of g.nodes) {
        const position = prior.get(node.d.id);
        if (position) { node.x = position.x; node.y = position.y; }
        else node.el.classList.add("enter");
      }
      if (selected && g.byId.has(selected)) g.select(selected);
      if (trace.nodes.length && !selected) g.fit(undefined, graphSignature ? 800 : 0, 1);
      graphSignature = signature;
    } else g.case = trace;
    renderResources(trace);
    panel.set(trace, result.state !== "done");
    const stepKey = (step: (typeof trace.steps)[0], i: number) => step.id ?? `#${i}`;
    const rows = trace.steps.map((step, i) => `<div class="line${step.state === "running" ? " active" : ""}${shownSteps.has(stepKey(step, i)) ? " seen" : ""}" data-state="${step.state}"><span class="gt">${step.state === "done" ? "✓" : step.state === "failed" ? "!" : "›"}</span><span class="tool t-${step.tool}">${esc(TOOL[step.tool].label)}</span><span class="txt"${step.detail && step.state !== "failed" ? ` title="${esc(step.detail)}"` : ""}><b>${esc(step.text)}</b>${step.detail && step.state === "failed" ? `<small>${esc(step.detail)}</small>` : ""}</span><span class="res">${step.state === "done" ? (step.found?.length ? String(step.found.length) : "") : esc(step.state ?? "pending")}</span></div>`).join(""); // one short line per step, readable as it streams in; details are a hover away, failures show theirs
    const scroll = consoleEl.querySelector(".term-body")?.scrollTop ?? 0;
    const dot = result.state === "failed" ? "r" : result.state === "running" ? "g live" : "g";
    consoleEl.innerHTML = `<div class="term-head"><span class="d ${dot}" title="Lookup ${esc(result.state)}"></span>${mock ? `<span class="file">MOCK · fictional data</span>` : ""}<button class="x" data-close-console aria-label="Hide trace console">hide</button></div><div class="term-body" role="log" aria-live="polite">${rows}${result.error ? `<p class="lookup-error" role="alert">${esc(result.error)}</p>` : ""}</div>`;
    $(".term-body", consoleEl).scrollTop = scroll;
    trace.steps.forEach((step, i) => shownSteps.add(stepKey(step, i)));
    const running = trace.steps.find((step) => step.state === "running");
    setStatus(running?.tool ?? null, running ? `${running.text}${running.detail ? ` · ${running.detail}` : ""}` : result.state === "running" ? "Waiting for live receipts…" : "");
  };
  update({ id: "lookup-pending", state: "running", error: null, input: { url: /^https?:\/\//i.test(text) ? text : null, text: /^https?:\/\//i.test(text) ? null : text }, kind: "text", started_at: new Date().toISOString(), finished_at: null, status: "insufficient", subject_id: null, origin_id: null, posts: [], accounts: [], pages: [], media: [], edges: [], clusters: [], spread: null, receipts: [], assessment: null, steps: [{ id: "input", tool: "you", label: "Start lookup", state: "running", detail: "Requesting live data; no coordination verdict yet.", found: [], started_at: null, ended_at: null }], notes: [] });
  try {
    const result = mock && import.meta.env.DEV
      ? await (await import("./data/mockLookup")).runMockLookup(update, controller.signal)
      : await startLookup(text, update, controller.signal);
    if (controller.signal.aborted || run !== tracing) return;
    const trace = current!;
    if (result.state === "failed") {
      setPhase("done");
      toast(result.error || "Lookup failed. Partial receipts remain available; no verdict was issued.");
      return;
    }
    if (!mock) {
      const existing = userCases.findIndex((item) => item.id === trace.id);
      if (existing >= 0) userCases.splice(existing, 1);
      userCases.unshift(trace);
      saveUserCases();
    }
    renderRail();
    panel.set(trace, false);
    setPhase("done");
    await showEvent(trace, run);
  } catch (error) {
    if (controller.signal.aborted || run !== tracing) return;
    const message = error instanceof Error ? error.message : "Lookup failed. Please try again later.";
    if (error instanceof LookupError && error.unavailable) {
      setStatus(null, "Lookup unavailable; trying the legacy web analysis…");
      toast(`${message} Trying web analysis instead.`);
      let trace: TraceCase | null = null;
      try { trace = await analyze(text); } catch { trace = null; }
      if (controller.signal.aborted || run !== tracing) return;
      if (!trace) {
        trace = simulateFromInput(text);
        toast("// live APIs not reachable: running a simulated trackdown");
      }
      userCases.unshift(trace);
      saveUserCases();
      runTrace(trace);
    } else {
      if (latest.result) update({ ...latest.result, state: "failed", error: message, steps: latest.result.steps.map((step) => step.state === "running" ? { ...step, state: "failed", detail: message } : step.state === "pending" ? { ...step, state: "skipped" } : step) });
      setPhase("done");
      toast(message);
    }
  } finally {
    if (lookupController === controller) {
      lookupController = null;
      app.classList.remove("lookup-active");
      tracing = 0;
      setStatus(null, "");
    }
  }
}

let toastT = 0;
function toast(msg: string) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  clearTimeout(toastT);
  toastT = window.setTimeout(() => t.remove(), 3400);
}

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  // focus stays inside the open phone sheet (a modal dialog)
  if (e.key === "Tab" && phoneMq.matches && app.classList.contains("sheet-open")) {
    const items = [...$("#panel").querySelectorAll<HTMLElement>('button:not([tabindex="-1"]), a[href], [tabindex="0"]')].filter((x) => x.offsetParent !== null);
    if (items.length) {
      const first = items[0];
      const last = items[items.length - 1];
      const inside = $("#panel").contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    }
    return;
  }
  if ((e.target as HTMLElement).closest("textarea,input")) return;
  // Space and Enter belong to the focused control; arrows belong to the tab list
  if ((e.key === " " || e.key === "Enter") && (e.target as HTMLElement).closest("button,a,[role=tab]")) return;
  if ((e.target as HTMLElement).closest("[role=tablist]") && e.key !== "Escape") return;
  if (e.key === "Escape") {
    if (eventEl) closeEvent();
    else if (tracing) skipFlag = true;
    else if (phoneMq.matches && app.classList.contains("sheet-open")) {
      app.classList.remove("sheet-open");
      syncDrawers();
      $("#peek").focus();
    } else if (g.selId) g.select(null);
    else if (app.classList.contains("rail-open")) {
      app.classList.remove("rail-open");
      $<HTMLElement>('[data-act="rail"]').focus();
    } else app.classList.remove("tools");
  }
  if (e.key === " ") {
    e.preventDefault();
    dock.toggle();
  }
  if (e.key === "f" || e.key === "F") g.fit();
  if (e.key === "t" || e.key === "T") setMode(g.mode === "timeline" ? "web" : "timeline");
  if (e.key === "n" || e.key === "N") {
    e.preventDefault();
    showHome();
  }
  if (e.key === "m" || e.key === "M") app.classList.toggle("tools");
  if (e.key === "+" || e.key === "=") g.zoomBy(1.3);
  if (e.key === "-") g.zoomBy(1 / 1.3);
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
syncDrawers();
const params = new URLSearchParams(location.search);
const first = simulatedCases().find((c) => c.id === params.get("case")) ?? simulatedCases()[0];
if (params.has("mockLookup") && import.meta.env.DEV) void startTrace("https://social.example/newsroom/status/100", true);
else if (params.get("lookup")) void startTrace(params.get("lookup")!);
else if (params.has("static")) openCase(first, "static");
else if (params.has("case")) runTrace(first);
else {
  setPhase("home");
  showHome();
}
listLive().then((rows) => {
  liveRows = rows;
  if (rows.length) renderRail();
});
