import "./styles.css";
import rhinoUrl from "./assets/rhino-1515.webp";
import { analyze, listLive, liveCase, type ClusterSummary } from "./data/api";
import { simulatedCases } from "./data/cases";
import { simulateFromInput } from "./data/simulate";
import { Graph, type Mode } from "./engine/graph";
import type { TraceCase } from "./types";
import { flipInto, preloadCoin, wait } from "./ui/coin";
import { thumbStyle } from "./ui/crop";
import { bannerReady, mountBanner } from "./ui/cvbanner";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtDur, fmtShort, ROLE, STATUS, TOOL } from "./ui/format";
import { icon } from "./ui/icons";
import { Panel } from "./ui/panel";
import { initTooltips } from "./ui/tooltip";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;
const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <button class="btn icon mobile-only rail-btn" data-act="rail" aria-label="Case files">${icon.menu}</button>
    <div class="wordmark">SHILL OR CHILL</div>
    <div class="case-head"><span class="label">trackdown</span><h1 id="title"></h1><span id="badges"></span></div>
    <div class="top-actions">
      <div class="seg" id="mode"><button data-mode="timeline" class="on">Timeline</button><button data-mode="web">Web</button></div>
      <button class="btn icon mobile-only" data-act="sheet" aria-label="Verdict">${icon.receipt}</button>
      <button class="btn solid" data-act="trace">${icon.search}<span class="lbl">Trace a post</span><span class="kbd">N</span></button>
    </div>
  </header>
  <aside class="rail"><div class="rail-inner" id="rail"></div></aside>
  <main class="stage" id="stage">
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="term hidden" id="console" data-ui></div>
    <div class="hint label" id="hint">drag cards · scroll to zoom · double-click to focus</div>
    <div class="legend min" id="legend" data-ui></div>
    <div class="hud" data-ui>
      <div class="zoom"><button data-z="in" aria-label="Zoom in">${icon.plus}</button><button data-z="out" aria-label="Zoom out">${icon.minus}</button><button data-z="fit" aria-label="Fit">${icon.fit}</button></div>
      <div class="minimap" id="minimap"><span class="mm-title label">map</span></div>
    </div>
    <div class="dock" id="dock" data-ui></div>
    <div class="vignette"></div>
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
  `<div class="legend-title"><span class="label">key</span><span>+</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="ln" style="--s:dashed;--c:#f5f5f5"></span>Match (copy, image)</div><div class="li"><span class="ln" style="--c:#b4b0a8"></span>Repost, quote</div><div class="li"><span class="ln" style="--s:dashed;--c:#7aa2f0"></span>Cites</div><div class="li"><span class="sw box" style="--c:#3fe07a"></span>Machine detection</div>`;
$("#legend").addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest(".legend-title")) {
    const l = $("#legend");
    l.classList.toggle("min");
    $(".legend-title span:last-child", l).textContent = l.classList.contains("min") ? "+" : "–";
  }
});

// ---------------------------------------------------------------- header
function renderResources(c: TraceCase) {
  const v = STATUS[c.status];
  $("#title").textContent = c.title;
  const tag = c.synthetic ? '<span class="tag sim">simulated</span>' : c.historical ? '<span class="tag hist">historical record</span>' : c.live ? '<span class="tag live">live</span>' : "";
  $("#badges").innerHTML = `<span style="display:flex;gap:10px;align-items:center"><span class="vchip" style="--vc:${v.css}">${v.short.toUpperCase()}</span>${tag}</span>`;
  document.title = `${c.title} · Shill or Chill`;
}

// ---------------------------------------------------------------- outliner
function railItem(c: TraceCase) {
  const sub = c.nodes.find((n) => n.id === c.submittedId);
  const ori = c.nodes.find((n) => n.id === c.originId);
  const th = c.historical ? { style: `background-image:url(${rhinoUrl});background-position:72% 40%;background-size:260%`, score: "1515" } : thumbStyle(c.id, 64);
  return `<button class="cat-item${current?.id === c.id ? " on" : ""}" data-case="${c.id}" style="--vc:${STATUS[c.status].css}">
    <span class="thumb" style="${th.style}" data-score="${th.score}"></span>
    <span class="vd">${STATUS[c.status].short} · ${esc(c.kicker)}</span>
    <span class="ttl">${esc(c.title)}</span>
    <span class="meta">${ori ? fmtShort(ori.t) : ""} → ${sub ? fmtShort(sub.t) : ""}${sub && ori ? ` · ${fmtDur(sub.t - ori.t)}` : ""}</span>
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
      (r) => `<button class="cat-item" data-live="${r.id}" style="--vc:${STATUS[r.status].css}"><span class="thumb" style="${thumbStyle("live" + r.id, 64).style}" data-score="${r.score.toFixed(4)}"></span>
      <span class="vd">${STATUS[r.status].short}${r.synthetic ? " · synthetic" : ""}</span><span class="ttl">Cluster ${r.id}</span>
      <span class="meta">${r.size ?? "?"} accounts · score ${r.score.toFixed(2)}</span></button>`,
    )
    .join("");
  $("#rail").innerHTML = `
    ${userCases.length ? `<div class="label"><span>your traces</span><span>${userCases.length}</span></div>${userCases.map(railItem).join("")}` : ""}
    ${live ? `<div class="label"><span>live from the monitor</span><span>${liveRows.length}</span></div>${live}` : ""}
    <div class="label"><span>case files</span><span>${sims.length}</span></div>
    ${sims.map(railItem).join("")}
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
  const mode = c.synthetic ? "--simulated" : c.historical ? "--archive" : "";
  return `<div class="term-head"><span class="d r"></span><span class="d y"></span><span class="d g"></span><span class="file">trackdown.sh ${mode}</span><button class="x" data-skip title="Skip (Esc)">${state === "run" ? "skip" : "close"}</button></div><div class="term-body"><div class="cmd">trackdown --post ${esc(c.submittedId.slice(0, 12))}${state === "done" ? " --done" : ""}</div></div>`;
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

// ---------------------------------------------------------------- verdict reveal: the Junction banner with the verdict as its wordmark
let eventEl: HTMLElement | null = null;
function closeEvent() {
  if (!eventEl) return;
  const e = eventEl;
  eventEl = null;
  e.classList.add("out");
  setTimeout(() => e.remove(), 480);
}
async function showEvent(c: TraceCase, run: number) {
  closeEvent();
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
  mountBanner($<HTMLCanvasElement>("canvas", el), { darken: c.historical ? 0.5 : 0.42, per: 70, src, focusX: 0.5 });
  el.addEventListener("click", closeEvent);
  await bannerReady;
  await wait(300);
  await flipInto($(".coin-slot", el), c.status, () => run !== tracing && tracing !== 0);
  $(".vw", el).animate([{ opacity: 0, letterSpacing: "0.7em" }, { opacity: 1, letterSpacing: "0.3em" }], { duration: 800, easing: "cubic-bezier(.16,1,.3,1)", fill: "forwards" });
  $(".vt", el).animate([{ opacity: 0 }, { opacity: 1 }], { duration: 600, delay: 350, fill: "forwards" });
  setTimeout(() => eventEl === el && closeEvent(), 5200);
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
  back.innerHTML = `<div class="modal" role="dialog" aria-label="Trace a post">
    <div class="banner"><canvas></canvas><div class="bt">SHILL OR CHILL</div><span class="credit">Botticelli, The Birth of Venus, c. 1485 · ORB keypoints</span></div>
    <div class="form">
      <p class="sub">Drop a post, a link or a handle. We walk it back to the earliest copy we can find, then look at how it spread.</p>
      <div class="drop"><textarea placeholder="Paste a post, a bsky.app link or @handle…"></textarea>
        <div class="row label">drop a screenshot · nemotron reads it<span class="file"></span></div></div>
      <div class="examples">${EXAMPLES.map((e) => `<button data-ex="${esc(e)}">${esc(e.length > 54 ? e.slice(0, 52) + "…" : e)}</button>`).join("")}</div>
      <div class="foot"><div class="fine">Asks the live API first. If it isn't reachable, you get a clearly labelled simulated trackdown.</div><button class="btn" data-x>Cancel</button><button class="btn solid" data-go>${sm(icon.search, 14)} Trace it</button></div>
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
      toast("// live API not reachable: running a simulated trackdown");
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
  t.className = "toast";
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
const params = new URLSearchParams(location.search);
const first = simulatedCases().find((c) => c.id === params.get("case")) ?? simulatedCases()[0];
if (params.has("static")) openCase(first, "static");
else {
  // title card: the painting is read by machine vision, then the trackdown starts
  const splash = document.createElement("div");
  splash.className = "splash";
  splash.innerHTML = `<canvas></canvas><div class="t"><div class="big">SHILL OR CHILL</div><div class="sub">// provenance for the feed · junction 2026</div></div>`;
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
