import "./styles.css";
import { analyze, listLive, liveCase, type ClusterSummary } from "./data/api";
import { simulatedCases } from "./data/cases";
import { simulateFromInput } from "./data/simulate";
import { Graph, type Mode } from "./engine/graph";
import type { TraceCase } from "./types";
import { coinImg, preloadCoin, tossCoin, wait } from "./ui/coin";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtDur, fmtShort, ROLE, roman, STATUS, TOOL } from "./ui/format";
import { icon } from "./ui/icons";
import { Panel } from "./ui/panel";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <div class="brand">
      <button class="cbtn icon mobile-only rail-btn" data-act="rail" aria-label="Catalogue">${icon.menu}</button>
      <div class="brand-coin" style="background-image:url(${coinImg("coordinated")})"></div>
      <div class="wordmark"><b>Shill</b> <span class="or">or</span> <b>Chill</b></div>
    </div>
    <span class="slash">trackdown</span>
    <div class="case-head"><h1 id="title"></h1><span id="badges"></span></div>
    <div class="top-actions">
      <div class="seg" id="mode"><button data-mode="timeline" class="on">Timeline</button><button data-mode="web">Web</button></div>
      <button class="cbtn icon mobile-only" data-act="sheet" aria-label="Catalogue entry">${icon.receipt}</button>
      <button class="cbtn solid" data-act="trace">${icon.search}<span class="lbl">Trace a post</span><span class="kbd">N</span></button>
    </div>
  </header>
  <aside class="rail"><div class="rail-inner" id="rail"></div></aside>
  <main class="stage" id="stage">
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="term hidden" id="console" data-ui></div>
    <div class="hint" id="hint">Drag the folios, scroll to zoom, double-click to look closer.</div>
    <div class="legend min" id="legend" data-ui></div>
    <div class="hud" data-ui>
      <div class="zoom"><button data-z="in" aria-label="Zoom in">${icon.plus}</button><button data-z="out" aria-label="Zoom out">${icon.minus}</button><button data-z="fit" aria-label="Fit">${icon.fit}</button></div>
      <div class="minimap" id="minimap"></div>
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
let tracing = 0; // trace run id; 0 = idle
let skipFlag = false;
let liveRows: ClusterSummary[] = [];
preloadCoin();

// ---------------------------------------------------------------- legend
$("#legend").innerHTML =
  `<div class="legend-title"><span>Key</span><span>+</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="ln" style="--s:dashed"></span>copied text</div><div class="li"><span class="ln" style="--s:dotted"></span>same image</div><div class="li"><span class="ln"></span>reposted, quoted</div><div class="li"><span class="ln" style="--s:dashed;--c:var(--lapis)"></span>cites</div>`;
$("#legend").addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest(".legend-title")) {
    const l = $("#legend");
    l.classList.toggle("min");
    $(".legend-title span:last-child", l).textContent = l.classList.contains("min") ? "+" : "–";
  }
});

// ---------------------------------------------------------------- rail
function spark(c: TraceCase, canvas: HTMLCanvasElement) {
  const dpr = Math.min(2, devicePixelRatio || 1);
  const w = 200;
  const h = 16;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const x = canvas.getContext("2d")!;
  x.scale(dpr, dpr);
  const ts = c.nodes.map((n) => n.t).sort((a, b) => a - b);
  const t0 = ts[0];
  // log-compressed time like the main axis
  const xs: number[] = [];
  let acc = 0;
  ts.forEach((t, i) => {
    if (i) acc += Math.log2(1 + (t - ts[i - 1]) / 60000);
    xs.push(acc);
  });
  const max = acc || 1;
  const col = STATUS[c.status].color;
  const bins = new Array(44).fill(0);
  xs.forEach((v) => bins[Math.min(43, Math.floor((v / max) * 43))]++);
  x.fillStyle = "rgba(29,24,19,0.35)";
  x.fillRect(0, h - 0.5, w, 0.5);
  const m = Math.max(...bins);
  bins.forEach((b, i) => {
    if (!b) return;
    const bh = Math.max(2, Math.sqrt(b / m) * (h - 2));
    x.fillStyle = col;
    x.globalAlpha = 0.35 + 0.65 * (b / m);
    x.fillRect(i * 4.5, h - bh, 1.2, bh);
  });
  void t0;
}

function railItem(c: TraceCase, i: number) {
  const sub = c.nodes.find((n) => n.id === c.submittedId);
  const ori = c.nodes.find((n) => n.id === c.originId);
  return `<button class="cat-item${current?.id === c.id ? " on" : ""}" data-case="${c.id}" style="--vc:${STATUS[c.status].css}">
    <span class="no">${roman(i + 1)}.</span>
    <span class="vd">${STATUS[c.status].short} · ${esc(c.kicker)}</span>
    <span class="mini-coin" style="background-image:url(${coinImg(c.status)})"></span>
    <span class="ttl">${esc(c.title)}</span>
    <span class="meta">${ori ? fmtShort(ori.t) : ""} to ${sub ? fmtShort(sub.t) : ""}${sub && ori ? `, ${fmtDur(sub.t - ori.t)}` : ""}</span>
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
    /* private mode: keep in memory only */
  }
}
function renderRail() {
  const sims = simulatedCases();
  const live = liveRows
    .map(
      (r, i) => `<button class="cat-item" data-live="${r.id}" style="--vc:${STATUS[r.status].css}"><span class="no">${roman(i + 1)}.</span>
      <span class="vd">${STATUS[r.status].short}${r.synthetic ? " · synthetic" : ""}</span><span class="mini-coin" style="background-image:url(${coinImg(r.status)})"></span><span class="ttl">Cluster ${r.id}</span>
      <span class="meta">${r.size ?? "?"} accounts, score ${r.score.toFixed(2)}</span></button>`,
    )
    .join("");
  $("#rail").innerHTML = `
    ${userCases.length ? `<div class="label">Your traces</div>${userCases.map(railItem).join("")}` : ""}
    ${live ? `<div class="label">Live from the monitor</div>${live}` : ""}
    <div class="label">The archive</div>
    ${sims.map(railItem).join("")}
    <p class="rail-note"><b>The archive is mostly simulated:</b> fictional accounts and <span class="mono" style="font-size:12px">.example</span> sites, built to show the ways a trace can end. The Rhinoceros is real history. ${live ? "Live clusters come straight from the detector." : "Live clusters appear here when the monitor API is reachable."}</p>`;
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

// ---------------------------------------------------------------- header
function setHeader(c: TraceCase) {
  $("#title").textContent = c.title;
  $("#badges").innerHTML = c.synthetic ? `<span class="tag sim">Simulated</span>` : c.historical ? `<span class="tag hist">Historical record</span>` : c.live ? `<span class="tag live">Live data</span>` : "";
  document.title = `${c.title} · Shill or Chill`;
}

const seg = $("#mode");
seg.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-mode]");
  if (!b) return;
  setMode(b.dataset.mode as Mode);
});
function setMode(m: Mode) {
  seg.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("on", (x as HTMLElement).dataset.mode === m));
  g.setMode(m);
  $(".ruler").style.opacity = m === "timeline" ? "1" : "0";
  setTimeout(() => g.fit(undefined, 1100), 380);
}

document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
  if (!a || a.closest("#panel")) return;
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
  panel.render();
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

let hintTimer = 0;
function hideHintSoon() {
  clearTimeout(hintTimer);
  hintTimer = window.setTimeout(() => ($("#hint").style.opacity = "0"), 6000);
}
stage.addEventListener("pointerdown", () => ($("#hint").style.opacity = "0"), { once: true });

// ---------------------------------------------------------------- opening a case
function openCase(c: TraceCase, how: "spread" | "static") {
  cancelTrace();
  current = c;
  setHeader(c);
  renderRail();
  g.load(c, { revealAll: how === "static" });
  panel.set(c, false);
  hideConsole();
  if (how === "spread") {
    // quick replay of the spread, camera follows
    dock.seek(0);
    g.fit([c.originId], 0, 0.9);
    setTimeout(() => {
      dock.speed = 1;
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

// ---------------------------------------------------------------- the trackdown choreography
function hideConsole() {
  consoleEl.classList.add("hidden");
}
function cancelTrace() {
  if (tracing) skipFlag = true;
}
function consoleHead(state: "run" | "done", c: TraceCase) {
  const file = c.synthetic ? "trackdown.sh --simulated" : c.historical ? "trackdown.sh --archive" : "trackdown.sh";
  return `<div class="term-head"><span class="d r"></span><span class="d y"></span><span class="d g"></span><span class="file">${file}</span><button class="x" data-skip title="Skip (Esc)">${state === "run" ? "skip" : "close"}</button></div><div class="term-body"><div class="cmd">trackdown --post ${esc(c.submittedId.slice(0, 12))}${state === "done" ? " --done" : ""}</div></div>`;
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
    await wait(12);
  }
  t.textContent = text;
  return row;
}

async function runTrace(c: TraceCase) {
  const run = ++tracing;
  skipFlag = false;
  current = c;
  setHeader(c);
  renderRail();
  g.load(c, { revealAll: false });
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
  const subPath = g.pathToOrigin(c.submittedId); // submitted -> ... -> origin

  for (const step of c.steps) {
    if (!alive()) break;
    const row = await typeLine(body, step.tool, step.text, run);
    const found = step.found ?? [];
    if (step.tool === "you") {
      reveal(found);
      g.focus(c.submittedId, 1.05, 0);
      const el = g.byId.get(c.submittedId)?.el;
      el?.classList.add("scanning");
      g.ring(c.submittedId, "#1d1813", 300, 1300);
      await pause(1300);
      el?.classList.remove("scanning");
      frontier.push(c.submittedId);
    } else if (found.length) {
      // reveal each found node, plus anything on the submitted->found path, plus same-cluster siblings
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
      // stagger big reveals (bursts) so they visibly pour in
      const chunk = newIds.length > 8 ? 4 : 1;
      for (let i = 0; i < newIds.length; i += chunk) {
        if (!alive()) break;
        reveal(newIds.slice(i, i + chunk), found.includes(newIds[i]) ? anchor : undefined);
        if (newIds.length > 1) await wait(newIds.length > 8 ? 45 : 180);
      }
      if (!alive()) break;
      for (const f of found) g.ring(f, ROLE[c.nodes.find((n) => n.id === f)!.role].color, 300, 1200);
      frontier.push(...found);
      await wait(120);
      g.fit([...(g.revealed ?? [])], 1100, 1);
      row.querySelector(".res")!.innerHTML = `<b>+${newIds.length}</b>${step.rejected ? ` −${step.rejected}` : ""}`;
      await pause(1300);
    } else {
      // analysis step: scan the current frontier card
      const id = frontier[frontier.length - 1];
      const el = id ? g.byId.get(id)?.el : null;
      el?.classList.add("scanning");
      await pause(1100);
      el?.classList.remove("scanning");
    }
    row.classList.remove("active");
  }
  if (run !== tracing) return;

  // origin moment
  if (alive() && c.originId) {
    g.setHighlight(new Set(g.pathToOrigin(c.submittedId)));
    g.ring(c.originId, ROLE.origin.color, 520, 2200);
    await typeLine(body, c.historical ? "archive" : "detector", `earliest known copy: ${fmtShort(c.nodes.find((n) => n.id === c.originId)!.t)}, ${Math.max(0, subPath.length - 1)} copies back`, run);
    await pause(1500);
    g.setHighlight(null);
  }
  // then the full spread pours in, in time order
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
  // finish (also the skip path)
  g.revealAll();
  dock.u = 1;
  consoleEl.innerHTML = consoleHead("done", c);
  const b2 = $(".term-body", consoleEl);
  const hops = Math.max(0, subPath.length - 1);
  b2.insertAdjacentHTML("beforeend", `<div class="line"><span class="gt">&gt;</span><span class="tool t-${c.historical ? "archive" : "gemini"}">verdict</span><span class="txt">${STATUS[c.status].word.toLowerCase()} // ${hops} copies back // ${c.nodes.length} posts</span><span class="res"></span></div>`);
  g.followFit(3200);
  const wasSkipped = skipFlag;
  skipFlag = false;
  await wait(wasSkipped ? 300 : 900);
  if (run !== tracing) return;
  panel.set(c, false);
  if (!wasSkipped) await tossCoin(stage, c.status, innerWidth < 860 ? null : panel.coinTarget(), () => run !== tracing);
  if (run === tracing) {
    tracing = 0;
    setTimeout(() => !tracing && hideConsole(), 5000);
  }
}

// ---------------------------------------------------------------- trace modal
const EXAMPLES = [
  "They're banning tap water on weekends. NOT a joke. Share before it gets deleted",
  "$GLOW partnership with a top payments network confirmed 🚀",
  "This flood photo is happening RIGHT NOW and the media is silent",
  "Harbour bridge closed both ways, loud bang from the east pillar",
];
function openModal() {
  if (document.querySelector(".modal-back")) return;
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal" role="dialog" aria-label="Trace a post">
    <div class="art"><div class="streak"></div></div>
    <div class="form">
      <span class="slash" style="color:var(--chrome-ink-2)">submit for attribution</span>
      <h2>Shill <i>or</i> chill?</h2>
      <div class="sub">Drop a post, a link or a handle. We walk it back to the earliest copy we can find, then look at how it spread.</div>
      <div class="drop"><textarea placeholder="Paste a post, a bsky.app link or @handle…" autofocus></textarea>
        <div class="row">// drop a screenshot here, Nemotron reads it<span class="file"></span></div></div>
      <div class="examples">${EXAMPLES.map((e) => `<button data-ex="${esc(e)}">${esc(e.length > 58 ? e.slice(0, 56) + "…" : e)}</button>`).join("")}</div>
      <div class="foot"><div class="fine">Asks the live API first. If it isn't reachable, you get a clearly labelled simulated trackdown.</div><button class="cbtn" data-x>Cancel</button><button class="cbtn solid" data-go>Trace it</button></div>
    </div>
  </div>`;
  document.body.appendChild(back);
  const ta = $<HTMLTextAreaElement>("textarea", back);
  const drop = $(".drop", back);
  let file: File | null = null;
  setTimeout(() => ta.focus(), 50);
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
    const btn = $<HTMLButtonElement>("[data-go]", back);
    btn.textContent = "Asking the API…";
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
  toastT = window.setTimeout(() => t.remove(), 3200);
}

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  if ((e.target as HTMLElement).closest("textarea,input")) return;
  if (e.key === "Escape") {
    if (tracing) skipFlag = true;
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
hideHintSoon();
const params = new URLSearchParams(location.search);
const first = simulatedCases().find((c) => c.id === params.get("case")) ?? simulatedCases()[0];
if (params.has("static")) openCase(first, "static");
else {
  // first visit: a short title card, then the full trackdown so the motion explains the product
  const splash = document.createElement("div");
  splash.className = "splash";
  splash.innerHTML = `<div class="bg"></div><div class="bars"></div><div class="t"><div class="big">Shill <i>or</i> Chill</div><div class="sub">// provenance for the feed &nbsp;·&nbsp; Junction 2026</div></div>`;
  document.body.appendChild(splash);
  let gone = false;
  const go = () => {
    if (gone) return;
    gone = true;
    splash.classList.add("out");
    setTimeout(() => splash.remove(), 950);
    runTrace(first);
  };
  splash.addEventListener("click", go);
  Promise.all([document.fonts.ready, wait(1900)]).then(go);
}
listLive().then((rows) => {
  liveRows = rows;
  if (rows.length) renderRail();
});
