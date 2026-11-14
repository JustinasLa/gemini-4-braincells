import "./styles.css";
import { showJudgingView } from "./judging";
import rhinoUrl from "./assets/rhino-1515.webp";
import fullCv from "./assets/venus-full-cv.json";
import venusFullUrl from "./assets/venus-full.webp";
import qrcode from "qrcode-generator";
import { analyze, liveCase } from "./data/api";
import { simulatedCases } from "./data/cases";
import { hasVerdict, IMAGE_TYPES, LookupError, lookupCase, readingReady, startLookup, type LookupRequest, type LookupResult } from "./data/lookup";
import { simulateFromInput } from "./data/simulate";
import { Graph } from "./engine/graph";
import { imageUrl, memeFinding } from "./data/imageTrace";
import type { TraceCase } from "./types";
import { preloadCoin, wait } from "./ui/coin";
import { bannerReady, mountBanner } from "./ui/cvbanner";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtDate, fmtShort, ROLE, roleOf, STATUS, TOOL } from "./ui/format";
import { icon } from "./ui/icons";
import { hasChain, Panel } from "./ui/panel";
import { initTooltips } from "./ui/tooltip";
import { comparisonSource } from "./ui/compare";
import { initMemeTrace } from "./ui/variations";
import { mountStory } from "./ui/story";
import { PANEL_MIN, clampPanel, panelMax, parseSaved, stepPanel } from "./ui/panelwidth";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <a class="wordmark" href="/" aria-label="Shill or Chill: home">SHILL OR CHILL</a>
    <div class="case-head"><h1 id="title"></h1><span id="badges"></span></div>
    <div class="top-actions">
      <button class=btn id=judge-menu aria-expanded=false>Explore</button>
      <span class=trace-wrap><button class=btn solid data-act=trace aria-label="Search">${icon.search}<span class=lbl>Search</span></button><button class=qr-btn data-act=qr aria-label=Open on phone (QR code) title=Open on phone>QR</button></span>
    </div>
  </header>
  <nav id="judge-links" hidden style="position:fixed;z-index:1000;top:58px;right:12px;background:#171717;padding:16px;gap:16px;flex-direction:column"><a href="?view=feed">Live cluster feed</a><a href="?view=metrics">Measured accuracy</a><a href="?view=methodology">Methodology &amp; limits</a><a href="?view=judge">Try a post</a></nav>
  <main class="stage" id="stage" tabindex="0" role="application" aria-roledescription="spread map" aria-label="Spread map. Arrow keys move between posts: left to the earlier copy, right to a later one, up and down between siblings.">
    <div class="sr-only" aria-live="polite" id="announce"></div>
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="term hidden" id="console" data-ui></div>
    <div class="status" id="status" data-ui inert><span class="st-tool"></span><span class="st-txt"></span><button class="st-skip label" data-skip>Skip</button></div>
    <div class="legend" id="legend" data-ui></div>
    <div class="hud" data-ui>
      <div class="zoom"><button data-z="in" aria-label="Zoom in">${icon.plus}</button><button data-z="out" aria-label="Zoom out">${icon.minus}</button><button data-z="fit" aria-label="Fit">${icon.fit}</button><button data-z="tools" aria-label="Key and map" aria-expanded="false" title="Key and map (M)">KEY</button></div>
      <div class="minimap" id="minimap"><span class="mm-title label">Map</span></div>
    </div>
    <div class="dock" id="dock" data-ui></div>
    <div class="empty-stage" id="empty" data-ui hidden><h2>No chain of posts to draw</h2><p>The detector grouped this live cluster by how its accounts behaved, not by copied posts we can lay out. The verdict and the evidence are in the panel.</p></div>
    <button class="peek" id="peek" data-ui data-act="sheet" aria-label="Show verdict and evidence"></button>
    <div class="vignette"></div>
  </main>
  <aside class="panel" id="panel"></aside>
  <div class="panel-grip" id="panel-grip" role="separator" aria-orientation="vertical" aria-controls="panel" aria-label="Resize panel" tabindex="0" title="Drag to resize · double-click to reset"></div>
</div>`;

const app = $(".app");
const story = mountStory(app);
// the picture being traced, as a local address: the story shows it before the server has it
let storyPreview: { url: string; lookup: string } | null = null;
const nav = $("#judge-links");
$("#judge-menu").addEventListener("click", () => { nav.hidden = !nav.hidden; nav.style.display = nav.hidden ? "none" : "flex"; $("#judge-menu").setAttribute("aria-expanded", String(!nav.hidden)); });
const view = new URLSearchParams(location.search).get("view");
if (["feed","metrics","methodology"].includes(view ?? "")) { app.hidden = true; app.style.display = String.fromCharCode(110,111,110,101); void showJudgingView(view!); }
// Closed drawers must not take keyboard focus; keep inert + aria-expanded in sync with the classes.
const phoneMq = matchMedia("(max-width: 860px)"); // the CSS breakpoint
const syncDrawers = () => {
  const phone = phoneMq.matches;
  const pnl = $("#panel");
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

// ---------------------------------------------------------------- panel width: drag the grip on its left edge
{
  const grip = $("#panel-grip");
  const KEY = "trackdown.panelW";
  // `want` is the width the user chose; what's applied is that, clamped to the current window
  let want: number | null = null;
  const apply = () => {
    const max = panelMax(innerWidth);
    grip.setAttribute("aria-valuemax", String(max));
    if (want == null) {
      app.style.removeProperty("--panel-w");
      grip.setAttribute("aria-valuenow", String(Math.round(parseFloat(getComputedStyle(app).getPropertyValue("--panel-w")) || 400)));
    } else {
      const w = clampPanel(want, innerWidth);
      app.style.setProperty("--panel-w", `${w}px`);
      grip.setAttribute("aria-valuenow", String(w));
    }
  };
  const current = () => clampPanel(want ?? (parseFloat(getComputedStyle(app).getPropertyValue("--panel-w")) || 400), innerWidth);
  const save = () => {
    try {
      want == null ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, String(want));
    } catch {}
  };
  try {
    want = parseSaved(localStorage.getItem(KEY));
  } catch {}
  grip.setAttribute("aria-valuemin", String(PANEL_MIN));
  apply();
  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    app.classList.add("resizing");
    const startX = e.clientX;
    const startW = current();
    const move = (ev: PointerEvent) => {
      want = clampPanel(startW + (startX - ev.clientX), innerWidth);
      apply();
    };
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      grip.removeEventListener("lostpointercapture", up);
      app.classList.remove("resizing");
      save();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
    grip.addEventListener("lostpointercapture", up);
  });
  grip.addEventListener("dblclick", () => {
    want = null;
    apply();
    save();
  });
  grip.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return;
    e.stopPropagation(); // the grip's keys are its own, never a graph or playback shortcut
    const w = stepPanel(current(), e.key, e.shiftKey, innerWidth);
    if (w == null) return;
    e.preventDefault();
    want = w;
    apply();
    save();
  });
  // a smaller window squeezes the panel; the chosen width comes back when the window grows again
  addEventListener("resize", apply);
}
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
preloadCoin();
initTooltips();

// ---------------------------------------------------------------- legend
$("#legend").innerHTML =
  `<div class="legend-title"><span class="label">Key</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source", "screenshotted"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="sw member" style="--c:${ROLE.amplifier.color}"></span>Cluster member · zoom in to read</div>` +
  `<div class="li"><span class="ln" style="--s:dashed;--c:#f5f5f5"></span>Match (copy, image)</div><div class="li"><span class="ln" style="--c:#b4b0a8"></span>Repost, quote</div><div class="li"><span class="ln" style="--s:dashed;--c:#7aa2f0"></span>Cites</div><div class="li"><span class="ln" style="--s:dotted;--c:${ROLE.screenshotted.color}"></span>Screenshot of a real post (checked, not a copy)</div><div class="legend-hint">Select a marker to read the post.</div>`;
initMemeTrace(() => current);


// ---------------------------------------------------------------- phases: home -> trace -> done
// Only what the moment needs is on screen: the canvas + one status line while tracing,
// the verdict panel and timeline once there is a verdict. Cases, map and key are opt-in.
type Phase = "home" | "trace" | "done";
function setPhase(p: Phase) {
  app.dataset.phase = p;
  app.classList.toggle("panel-open", p === "done");
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
/** The mobile summary: the verdict when there is one; for a lookup without one, the image finding
 *  (or no chip at all), never "Can't tell". */
function chipOf(c: TraceCase): { short: string; css: string } | null {
  if (hasVerdict(c)) return STATUS[c.status];
  const finding = c.lookup ? memeFinding(c.lookup) : null;
  return finding && finding !== "pending" ? { short: finding.word, css: finding.color } : null;
}
function renderResources(c: TraceCase) {
  if (storyPreview && c.lookup && storyPreview.lookup === "lookup-pending" && c.lookup.id !== "lookup-pending") storyPreview.lookup = c.lookup.id;
  story.update(c, storyPreview && c.lookup && storyPreview.lookup === c.lookup.id ? storyPreview.url : undefined);
  const v = chipOf(c);
  $("#title").textContent = c.title;
  const tag = c.mock ? '<span class="tag sim">Mock · fictional data</span>' : c.synthetic ? '<span class="tag sim">Simulated</span>' : c.historical ? '<span class="tag hist">Historical record</span>' : "";
  $("#badges").innerHTML = tag;
  const origin = c.synthetic ? "Simulated" : c.historical ? "Historical record" : c.live ? "Live" : "";
  const peek = $("#peek");
  peek.style.setProperty("--vc", v?.css ?? "var(--unsure)");
  peek.innerHTML = `${v ? `<span class="vchip" style="--vc:${v.css}">${esc(v.short)}</span>` : ""}<span class="pk-t"><b>${esc(c.title)}</b><span>${origin}</span></span><span class="pk-go">Details ↑</span>`;
  const empty = !hasChain(c) && !c.findingsOnly && !c.lookup; // a link lookup draws what it collected
  if (empty) app.dataset.empty = "";
  else delete app.dataset.empty;
  // search-only results have no known dates: no time axis, no playback
  if (c.findingsOnly) app.dataset.notime = "";
  else delete app.dataset.notime;
  $("#empty").hidden = !empty;
  document.title = `${c.title} · Trackdown · Shill or Chill`;
}

// ---------------------------------------------------------------- actions
// An uploaded image lives on the server only while its lookup does (1 h): a saved image trace
// reopened later says so instead of showing a broken image.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !/\/api\/lookup\/[^/]+\/image$/.test(img.src)) return;
  const box = img.closest(".media");
  if (box) box.outerHTML = `<div class="media photo"><span class="cap">Uploaded image · no longer kept on the server (uploads are kept for 1 hour)</span></div>`;
}, true);

document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
  if (!a || a.closest("#panel") || a.closest(".event")) return;
  const act = a.dataset.act;
  if (act === "trace") showHome();
  if (act === "qr") showQr();
  if (act === "sheet") {
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
  $("#announce").textContent = n ? `${n.author.name}, ${roleOf(n.role).label}${n.undated || n.timeUnknown ? "" : `, ${fmtDate(n.t)}`}` : "";
};
g.onHover = (id) => {
  if (tracing && app.dataset.phase !== "done") return; // a settled lookup may still be adding a second reading
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
  return `<div class="term-head"><span class="d r"></span><span class="d y"></span><span class="d g"></span><span class="file">trackdown.sh ${mode}</span><button class="x" data-skip title="Skip (Esc)">${state === "run" ? "Skip" : "Close"}</button></div><div class="term-body"><div class="cmd">trackdown --post ${esc(c.submittedId.slice(0, 12))}${state === "done" ? " --done" : ""}</div></div>`;
}
const statusEl = $("#status");
statusEl.addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest("[data-skip]")) {
    skipFlag = true;
    if (lookupController) {
      lookupController.abort();
      setPhase("done");
    }
  }
});
consoleEl.addEventListener("click", (event) => {
  if ((event.target as HTMLElement).closest("[data-close-console]")) hideConsole();
});
function setStatus(tool: keyof typeof TOOL | null, text: string) {
  statusEl.classList.toggle("on", !!text);
  statusEl.inert = !text; // a hidden status line must not hold a focus stop
  const tl = $(".st-tool", statusEl);
  tl.className = "st-tool";
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
        g.ring(f, roleOf(c.nodes.find((n) => n.id === f)?.role).color, 300, 1500);
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
  const finale = wasSkipped ? null : revealOf(c);
  if (finale && finale !== "wait") {
    closeEvent();
    afterReveal = () => setPhase("done");
    showEvent(c, finale);
  } else setPhase("done");
  if (run === tracing) tracing = 0;
}

// ---------------------------------------------------------------- reveal: the Junction banner with the finding as its wordmark
/** What the finale shows. A coordination verdict shows its word (no coin); a lookup with no verdict shows what
 *  the image search found about the picture instead (Original / Copy / Edited / First sighting). */
interface Reveal { word: string; tag: string; css: string; image?: string }
function revealOf(c: TraceCase): Reveal | "wait" | null {
  if (hasVerdict(c)) {
    const v = STATUS[c.status];
    return { word: v.word, tag: `${c.tag ?? v.tag}${c.synthetic ? " · simulated case" : c.historical ? " · historical record" : ""}`, css: v.css };
  }
  const finding = c.lookup ? memeFinding(c.lookup) : null;
  if (finding === "pending") return "wait";
  if (!finding) return null; // a lookup with no image search and no verdict: nothing to announce
  return { word: finding.word, tag: `${finding.tag}${c.mock ? " · mock" : ""}`, css: finding.color, image: imageUrl(comparisonSource(c)) };
}
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
async function showEvent(c: TraceCase, r: Reveal) {
  const el = document.createElement("div");
  el.className = "reveal";
  el.style.setProperty("--vc", r.css);
  const slot = r.image ? `<img class="meme-slot" alt="The submitted picture" src="${esc(r.image)}" referrerpolicy="no-referrer" draggable="false">` : "";
  el.innerHTML = `<canvas></canvas><div class="center">${slot}<div class="vw${r.word.length > 8 ? " long" : ""}">${esc(r.word.toUpperCase())}</div><div class="vt">${esc(r.tag)}</div></div><span class="tap label">Click to continue</span><span class="credit">${c.historical ? "Dürer, Rhinocerus, 1515" : "Botticelli, The Birth of Venus, c. 1485 · ORB keypoints"}</span>`;
  el.querySelector(".meme-slot")?.addEventListener("error", (e) => (e.target as HTMLElement).remove());
  stage.appendChild(el);
  eventEl = el;
  let src: HTMLImageElement | undefined;
  if (c.historical) {
    src = new Image();
    src.src = rhinoUrl;
  }
  mountBanner($<HTMLCanvasElement>("canvas", el), { darken: c.historical ? 0.5 : 0.42, per: 70, src, focusX: 0.5, avoid: () => [...el.querySelectorAll(".meme-slot, .vw, .vt, .tap")] });
  el.addEventListener("click", closeEvent);
  await bannerReady;
  await wait(300);
  el.querySelector(".meme-slot")?.animate([{ opacity: 0, transform: "scale(.92)" }, { opacity: 1, transform: "scale(1)" }], { duration: 600, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
  $(".vw", el).animate([{ opacity: 0, letterSpacing: "0.7em" }, { opacity: 1, letterSpacing: "0.3em" }], { duration: 800, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
  $(".vt", el).animate([{ opacity: 0 }, { opacity: 1 }], { duration: 600, delay: 350, fill: "forwards" });
  setTimeout(() => eventEl === el && closeEvent(), 5200);
}

// ---------------------------------------------------------------- home: the banner, one input, a few cases
let homeEl: HTMLElement | null = null;
let stopHomeBanner: (() => void) | null = null;
// Phone hand-off: the site runs on a bare IP, so scan instead of typing it.
function showQr() {
  const url = location.href;
  const qr = qrcode(0, "M");
  qr.addData(url);
  qr.make();
  const dlg = document.createElement("dialog");
  dlg.className = "qr-dialog";
  dlg.setAttribute("aria-label", "Open this page on your phone");
  dlg.innerHTML = `<div class="qr-code">${qr.createSvgTag({ cellSize: 6, margin: 2, scalable: true })}</div><p class="qr-url">${esc(url)}</p><button class="btn" data-close>Close</button>`;
  dlg.addEventListener("click", (e) => { if (e.target === dlg || (e.target as HTMLElement).closest("[data-close]")) dlg.close(); });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
}
function showHome() {
  cancelTrace();
  closeEvent();
  if (homeEl) return;
  const cases = simulatedCases();
  const sims = cases.filter((c) => !c.historical);
  const hist = cases.filter((c) => c.historical);
  const caseBtn = (c: TraceCase) => `<button data-hcase="${c.id}">${esc(c.title)}${c.historical ? ' <em>Historical record</em>' : ""}</button>`;
  homeEl = document.createElement("div");
  homeEl.className = "home";
  homeEl.innerHTML = `<canvas></canvas>
    <div class="home-c">
      <div class="home-wm">SHILL OR CHILL</div>
      <p class="home-tag">Drop a meme. Find the original and what changed.</p>
      <form class="home-form"><div class="home-q"><span class="home-att" hidden><img alt="" /><span class="nm">Pasted image</span><button type="button" class="x" aria-label="Remove the image">×</button></span><input name="q" aria-label="Post text, link or handle" placeholder="Drop a meme or paste a link" autocomplete="off" /></div><label class="btn home-upload" title="Upload an image: a meme or a screenshot (JPEG, PNG, WebP, GIF)">${icon.image}<span class="lbl">Upload</span><input type="file" accept="${IMAGE_TYPES.join(",")}" aria-label="Upload an image to search" /></label><span class="trace-wrap"><button class="btn solid" type="submit">Search</button><button type="button" class="qr-btn" data-act="qr" aria-label="Open on phone (QR code)" title="Open on phone">QR</button></span></form>
      <details class="home-ex"><summary class="label">Simulated examples</summary>${[...sims, ...hist].map(caseBtn).join("")}</details>
    </div>
    <span class="credit">Botticelli, c. 1485</span>`;
  document.body.appendChild(homeEl);
  const img = new Image();
  img.src = venusFullUrl;
  const home = homeEl;
  stopHomeBanner = mountBanner($<HTMLCanvasElement>("canvas", homeEl), { darken: 0.4, per: 90, delay: 300, src: img, cv: fullCv as never, focusX: 0.5, focusY: 0.25, avoid: () => [...home.querySelectorAll(".home-wm, .home-tag, .home-form, .home-ex, .credit")] });
  const input = $<HTMLInputElement>("input", homeEl);
  // Do not summon the on-screen keyboard just because the home screen opened.
  // Desktop keeps the quick keyboard workflow; touch users can choose the field when ready.
  if (!matchMedia("(pointer: coarse)").matches) setTimeout(() => input.focus(), 400);
  // a pasted image sits in the search box until Search (or Enter) sends it
  const q = $<HTMLElement>(".home-q", homeEl);
  const att = $<HTMLElement>(".home-att", homeEl);
  const placeholder = input.placeholder;
  let attached: File | null = null;
  let attachedUrl = "";
  const detach = () => {
    if (attachedUrl) URL.revokeObjectURL(attachedUrl);
    attached = null;
    attachedUrl = "";
    att.hidden = true;
    q.classList.remove("has-img");
    input.readOnly = false;
    input.placeholder = placeholder;
  };
  const attach = (f: File) => {
    if (!IMAGE_TYPES.includes(f.type)) {
      toast(`${f.type.replace("image/", "").toUpperCase() || "That"} images can't be read here. Use a JPEG, PNG, WebP or GIF.`);
      return;
    }
    detach();
    attached = f;
    attachedUrl = URL.createObjectURL(f);
    $<HTMLImageElement>("img", att).src = attachedUrl;
    $<HTMLElement>(".nm", att).textContent = f.name && !/^image\.\w+$/.test(f.name) ? f.name : "Pasted image";
    att.hidden = false;
    q.classList.add("has-img");
    input.value = "";
    input.readOnly = true; // one thing at a time: the image, or text after removing it
    input.placeholder = "Press Enter";
    input.focus();
  };
  $<HTMLButtonElement>(".x", att).addEventListener("click", () => { detach(); input.focus(); });
  input.addEventListener("keydown", (e) => {
    if (attached && (e.key === "Backspace" || e.key === "Delete")) { e.preventDefault(); detach(); }
  });
  $("form", homeEl).addEventListener("submit", (e) => {
    e.preventDefault();
    if (attached) traceImage(attached);
    else startTrace(input.value);
  });
  // an image comes in three ways: the Image button, a drop anywhere on the home, a paste
  const imageOf = (files?: FileList | null, items?: DataTransferItemList | null) =>
    [...(files ?? [])].find((f) => f.type.startsWith("image/"))
    ?? [...(items ?? [])].find((i) => i.kind === "file" && i.type.startsWith("image/"))?.getAsFile() ?? null;
  // checked here, before the home screen goes: a HEIC or SVG gets a toast, not a failed trace
  const traceImage = (f: File) => IMAGE_TYPES.includes(f.type) ? void startTrace(f)
    : toast(`${f.type.replace("image/", "").toUpperCase() || "That"} images can't be read here. Use a JPEG, PNG, WebP or GIF.`);
  const file = $<HTMLInputElement>('input[type="file"]', homeEl);
  file.addEventListener("change", () => { const f = imageOf(file.files); file.value = ""; if (f) traceImage(f); });
  // Ctrl+V / ⌘V puts a copied image into the search box: in the box, or anywhere on the home
  // screen (focus is often elsewhere after a click on the page)
  const pasteImage = (e: ClipboardEvent) => {
    if (homeEl !== home) return document.removeEventListener("paste", pasteImage);
    const f = imageOf(e.clipboardData?.files, e.clipboardData?.items);
    if (f) { e.preventDefault(); attach(f); return; }
    if (e.target === input || attached) return; // text into the box is the browser's own paste
    const text = e.clipboardData?.getData("text/plain")?.trim();
    if (text) { e.preventDefault(); input.value = text; input.focus(); }
  };
  document.addEventListener("paste", pasteImage);
  homeEl.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer?.types ?? [])].includes("Files")) return;
    e.preventDefault();
    home.classList.add("drop");
  });
  homeEl.addEventListener("dragleave", (e) => { if (!e.relatedTarget || !home.contains(e.relatedTarget as Node)) home.classList.remove("drop"); });
  homeEl.addEventListener("drop", (e) => {
    e.preventDefault();
    home.classList.remove("drop");
    const f = imageOf(e.dataTransfer?.files, e.dataTransfer?.items);
    if (f) traceImage(f);
    else toast("Drop an image file (JPEG, PNG, WebP or GIF).");
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
async function startTrace(raw: LookupRequest, mock = false) {
  const image = typeof raw === "string" ? null : raw;
  const text = typeof raw === "string" ? raw.trim() : "";
  if (!text && !image) {
    homeEl?.querySelector(".home-form")?.animate([{ transform: "translateX(0)" }, { transform: "translateX(-6px)" }, { transform: "translateX(6px)" }, { transform: "translateX(0)" }], { duration: 300 });
    return;
  }
  cancelTrace();
  closeEvent();
  hideHome();
  if (storyPreview) URL.revokeObjectURL(storyPreview.url);
  storyPreview = image ? { url: URL.createObjectURL(image), lookup: "lookup-pending" } : null;
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
  consoleEl.innerHTML = ""; // rows of an earlier lookup must not count as already shown
  let graphSignature = "";
  // Each poll re-renders the console; only steps not shown before slide in, so it does not flash.
  const shownSteps = new Set<string>();
  let settled = false;
  // the finale shows once: at settle, or later when the image search that decides it finishes
  let revealed = false;
  const reveal = (trace: TraceCase) => {
    // an uploaded image is nearly always a copy: no verdict or Copy banner, straight to the trace
    if (revealed || image) return;
    const r = revealOf(trace);
    if (r === "wait") return;
    revealed = true;
    if (r) void showEvent(trace, r);
  };
  const latest: { result: LookupResult | null } = { result: null };
  // The primary reading is in: show the finished trace now. A second reading that is still
  // running is added later by update() without re-running this reveal or refitting the graph.
  const settle = (trace: TraceCase) => {
    if (settled) return;
    settled = true;
    setStatus(null, "");
    setPhase("done");
    reveal(trace);
  };
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
    // a failed job still shows a primary reading that arrived before the failure
    const ready = readingReady(result) && (result.state !== "failed" || !!result.assessment);
    panel.set(trace, !ready);
    const stepKey = (step: (typeof trace.steps)[0], i: number) => step.id ?? `#${i}`;
    // only what has happened or is happening gets a line; waiting and skipped steps fold into one line each
    const row = (step: (typeof trace.steps)[0], i: number) => `<div class="line${step.state === "running" ? " active" : ""}${shownSteps.has(stepKey(step, i)) ? " seen" : ""}" data-state="${step.state}"><span class="gt">${step.state === "done" ? "✓" : step.state === "failed" ? "!" : "›"}</span><span class="tool t-${step.tool}">${esc(TOOL[step.tool].label)}</span><span class="txt" title="${esc(step.detail && step.state !== "failed" ? `${step.text}\n${step.detail}` : step.text)}"><b>${esc(step.text)}</b>${step.detail && step.state === "failed" ? `<small>${esc(step.detail)}</small>` : ""}</span><span class="res">${step.state === "done" ? (step.found?.length ? String(step.found.length) : "") : step.state === "running" ? "" : esc(step.state ?? "")}</span></div>`;
    const folded = (state: "pending" | "skipped", label: string) => {
      const steps = trace.steps.filter((step) => (step.state ?? "pending") === state);
      return steps.length ? `<div class="line fold seen" data-state="${state}" title="${esc(steps.map((step) => step.text).join("\n"))}"><span class="gt">·</span><span class="txt">${steps.length} ${label}</span></div>` : "";
    };
    const rows = trace.steps.map((step, i) => ["done", "running", "failed"].includes(step.state ?? "pending") ? row(step, i) : "").join("") + folded("pending", "queued") + folded("skipped", "skipped");
    const scroll = consoleEl.querySelector(".term-body")?.scrollTop ?? 0;
    consoleEl.innerHTML = `<div class="term-head">${mock ? `<span class="file">Mock · fictional data</span>` : ""}<button class="x" data-close-console aria-label="Hide trace console">Hide</button></div><div class="term-body" role="log" aria-live="polite">${rows}${result.error ? `<p class="lookup-error" role="alert">${esc(result.error)}</p>` : ""}</div>`;
    $(".term-body", consoleEl).scrollTop = scroll;
    trace.steps.forEach((step, i) => shownSteps.add(stepKey(step, i)));
    if (ready && result.state === "running") settle(trace);
    // a finale held back for the image search shows when that search finishes, even mid-job
    if (settled && result.state !== "failed") reveal(trace);
    if (!settled) {
      const running = trace.steps.find((step) => step.state === "running");
      setStatus(running?.tool ?? null, running ? `${running.text}${running.detail ? ` · ${running.detail}` : ""}` : result.state === "running" ? "Waiting for live receipts…" : "");
    }
  };
  update({ id: "lookup-pending", state: "running", error: null, input: image ? { url: null, text: null, image: { sha256: "", mime: image.type, bytes: image.size } } : { url: /^https?:\/\//i.test(text) ? text : null, text: /^https?:\/\//i.test(text) ? null : text }, kind: image ? "image" : "text", started_at: new Date().toISOString(), finished_at: null, status: "insufficient", subject_id: null, origin_id: null, posts: [], accounts: [], pages: [], media: [], edges: [], clusters: [], spread: null, receipts: [], assessment: null, steps: [{ id: "input", tool: "you", label: "Start lookup", state: "running", detail: "Requesting live data; no coordination verdict yet.", found: [], started_at: null, ended_at: null }], notes: [] });
  try {
    const result = mock && import.meta.env.DEV
      ? await (await import("./data/mockLookup")).runMockLookup(update, controller.signal)
      : await startLookup(image ?? text, update, controller.signal);
    if (controller.signal.aborted || run !== tracing) return;
    const trace = current!;
    if (result.state === "failed") {
      if (!settled) setPhase("done");
      toast(result.error || "Lookup failed. Partial receipts remain available; no verdict was issued.");
      return;
    }
    if (settled) {
      return;
    }
    settle(trace);
  } catch (error) {
    if (controller.signal.aborted || run !== tracing) return;
    const message = error instanceof Error ? error.message : "Lookup failed. Please try again later.";
    // once the primary reading is shown, a late failure keeps that trace instead of starting over
    // the legacy web analysis and the simulation take text; an image has no fallback
    if (error instanceof LookupError && error.unavailable && !settled && !image) {
      setStatus(null, "Lookup unavailable; trying the legacy web analysis…");
      toast(`${message} Trying web analysis instead.`);
      let trace: TraceCase | null = null;
      try { trace = await analyze(text); } catch { trace = null; }
      if (controller.signal.aborted || run !== tracing) return;
      if (!trace) {
        trace = simulateFromInput(text);
        toast("// live APIs not reachable: running a simulated trackdown");
      }
      runTrace(trace);
    } else {
      if (latest.result) update({ ...latest.result, state: "failed", error: message, steps: latest.result.steps.map((step) => step.state === "running" ? { ...step, state: "failed", detail: message } : step.state === "pending" ? { ...step, state: "skipped" } : step) });
      if (!settled) setPhase("done");
      toast(settled ? `${message} The readings received so far stay on screen.` : message);
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
    else app.classList.remove("tools");
  }
  if (e.key === " ") {
    e.preventDefault();
    dock.toggle();
  }
  if (e.key === "," || e.key === ".") dock.step(e.key === "," ? -1 : 1);
  if (e.key === "f" || e.key === "F") g.fit();
  if (e.key === "n" || e.key === "N") {
    e.preventDefault();
    showHome();
  }
  if (e.key === "m" || e.key === "M") app.classList.toggle("tools");
  if (e.key === "+" || e.key === "=") g.zoomBy(1.3);
  if (e.key === "-") g.zoomBy(1 / 1.3);
  if (/^[1-9]$/.test(e.key)) {
    const c = simulatedCases()[Number(e.key) - 1];
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
syncDrawers();
const params = new URLSearchParams(location.search);
const first = simulatedCases().find((c) => c.id === params.get("case")) ?? simulatedCases()[0];
if (params.has("mockLookup") && import.meta.env.DEV) void startTrace("https://social.example/newsroom/status/100", true);
else if (params.get("lookup")) void startTrace(params.get("lookup")!);
else if (/^\d+$/.test(params.get("cluster") ?? "")) liveCase(Number(params.get("cluster"))).then((c) => openCase(c, "spread"), () => { toast("Couldn't load that cluster from the API"); openCase(first, "static"); });
else if (params.has("static")) openCase(first, "static");
else if (params.has("case")) runTrace(first);
else {
  setPhase("home");
  showHome();
}
