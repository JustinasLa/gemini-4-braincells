import "./styles.css";
import { analyze, listLive, liveCase, type ClusterSummary } from "./data/api";
import { simulatedCases } from "./data/cases";
import { simulateFromInput } from "./data/simulate";
import { Graph, type Mode } from "./engine/graph";
import type { TraceCase } from "./types";
import { coinImg, preloadCoin, tossCoin, wait } from "./ui/coin";
import { Dock, drawRuler, Minimap } from "./ui/dock";
import { esc, fmtDur, fmtShort, ROLE, STATUS, TOOL, toolChip } from "./ui/format";
import { icon } from "./ui/icons";
import { Panel } from "./ui/panel";

const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector<T>(s)!;

document.querySelector("#app")!.innerHTML = `
<div class="app">
  <header class="topbar">
    <div class="brand">
      <button class="btn icon mobile-only rail-btn" data-act="rail" aria-label="Trackdowns">${icon.menu}</button>
      <div class="brand-coin" style="background-image:url(${coinImg("coordinated")})"></div>
      <div class="wordmark"><i class="s">Shill</i> <span class="or">or</span> <i class="c">Chill</i></div>
      <span class="brand-tag">Trackdown</span>
    </div>
    <div class="case-head"><span class="kicker" id="kicker"></span><h1 id="title"></h1><span id="badges"></span></div>
    <div class="top-actions">
      <div class="seg" id="mode"><span class="seg-thumb"></span><button data-mode="timeline" class="on">${icon.timeline.replace("<svg", '<svg width="14" height="14"')}<span class="lbl">Timeline</span></button><button data-mode="web">${icon.web2.replace("<svg", '<svg width="14" height="14"')}<span class="lbl">Web</span></button></div>
      <button class="btn icon mobile-only" data-act="sheet" aria-label="Receipts">${icon.receipt}</button>
      <button class="btn primary" data-act="trace">${icon.search.replace("<svg", '<svg width="15" height="15"')}<span class="lbl">Trace a post</span><span class="kbd">N</span></button>
    </div>
  </header>
  <aside class="rail"><div class="rail-inner" id="rail"></div></aside>
  <main class="stage" id="stage">
    <div class="ruler" data-ui><canvas id="ruler"></canvas></div>
    <div class="console hidden" id="console" data-ui></div>
    <div class="hint" id="hint">drag cards · scroll to zoom · drag the canvas to pan · double-click to focus</div>
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
  `<div class="legend-title"><span>Legend</span><span>+</span></div>` +
  (["origin", "mutation", "submitted", "amplifier", "organic", "relay", "source"] as const).map((r) => `<div class="li"><span class="sw" style="--c:${ROLE[r].color}"></span>${ROLE[r].label}</div>`).join("") +
  `<div class="li"><span class="ln" style="--s:dashed;--c:#ff4757"></span>copied text</div><div class="li"><span class="ln" style="--s:dotted;--c:#ff8a4c"></span>same image</div>`;
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
  const w = 220;
  const h = 22;
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
  const col = STATUS[c.status].css.startsWith("var") ? getComputedStyle(document.documentElement).getPropertyValue(STATUS[c.status].css.slice(4, -1)) : STATUS[c.status].css;
  const bins = new Array(44).fill(0);
  xs.forEach((v) => bins[Math.min(43, Math.floor((v / max) * 43))]++);
  const m = Math.max(...bins);
  bins.forEach((b, i) => {
    if (!b) return;
    const bh = Math.max(2, Math.sqrt(b / m) * (h - 2));
    x.fillStyle = col;
    x.globalAlpha = 0.25 + 0.6 * (b / m);
    x.fillRect(i * 5, h - bh, 3.4, bh);
  });
  void t0;
}

function railItem(c: TraceCase) {
  const sub = c.nodes.find((n) => n.id === c.submittedId);
  const ori = c.nodes.find((n) => n.id === c.originId);
  const accounts = new Set(c.nodes.map((n) => n.author.handle)).size;
  return `<button class="case-item${current?.id === c.id ? " on" : ""}" data-case="${c.id}" style="--vc:${STATUS[c.status].css}">
    <span class="mini-coin" style="background-image:url(${coinImg(c.status)})"></span>
    <span class="k">${STATUS[c.status].short} · ${esc(c.kicker)}</span>
    <span class="ttl">${esc(c.title)}</span>
    <span class="meta"><span>${ori ? fmtShort(ori.t) : ""} → ${sub ? fmtShort(sub.t) : ""}</span><span>${accounts} accts</span>${sub && ori ? `<span>${fmtDur(sub.t - ori.t)}</span>` : ""}</span>
    <canvas class="spark"></canvas>
  </button>`;
}

const userCases: TraceCase[] = [];
function renderRail() {
  const sims = simulatedCases();
  const live = liveRows
    .map(
      (r) => `<button class="case-item" data-live="${r.id}" style="--vc:${STATUS[r.status].css}"><span class="mini-coin" style="background-image:url(${coinImg(r.status)})"></span>
      <span class="k">${STATUS[r.status].short}${r.synthetic ? " · synthetic" : ""}</span><span class="ttl">Cluster #${r.id}</span>
      <span class="meta"><span>${r.size ?? "?"} accts</span><span>score ${r.score.toFixed(2)}</span></span></button>`,
    )
    .join("");
  $("#rail").innerHTML = `
    ${userCases.length ? `<h3>Your traces <span class="count">${userCases.length}</span></h3>${userCases.map(railItem).join("")}` : ""}
    ${live ? `<h3><span class="pill live"><span class="blip"></span>live</span> From the monitor <span class="count">${liveRows.length}</span></h3>${live}` : ""}
    <h3>Trackdown archive <span class="count">${sims.length}</span></h3>
    ${sims.map(railItem).join("")}
    <p class="rail-note"><b>Archive cases are simulated</b>: fictional accounts and <code>.example</code> sites, built to show the four ways a trace can end. ${live ? "Live clusters come straight from the detector." : "Live clusters appear here when the monitor API is reachable."}</p>`;
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
  $("#kicker").textContent = c.kicker;
  $("#title").textContent = c.title;
  $("#badges").innerHTML = c.synthetic ? `<span class="pill sim"><span class="blip"></span>Simulated</span>` : c.live ? `<span class="pill live"><span class="blip"></span>Live data</span>` : "";
  document.title = `${c.title} · Shill or Chill`;
}

const seg = $("#mode");
function placeThumb() {
  const on = $(".on", seg);
  const th = $(".seg-thumb", seg);
  th.style.left = `${on.offsetLeft}px`;
  th.style.width = `${on.offsetWidth}px`;
}
seg.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-mode]");
  if (!b) return;
  setMode(b.dataset.mode as Mode);
});
function setMode(m: Mode) {
  seg.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("on", (x as HTMLElement).dataset.mode === m));
  placeThumb();
  g.setMode(m);
  $(".ruler").style.opacity = m === "timeline" ? "1" : "0";
  setTimeout(() => g.fit(undefined, 1100), 380);
}
requestAnimationFrame(placeThumb);

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
      setTimeout(() => g.fit(undefined, 1400), 1400);
      setTimeout(() => current === c && g.fit(undefined, 1200), 3400);
    }, 350);
  } else {
    dock.u = 1;
    g.fit(undefined, 0);
  }
}

// ---------------------------------------------------------------- the trackdown choreography
function hideConsole() {
  consoleEl.classList.add("hidden");
}
function cancelTrace() {
  if (tracing) skipFlag = true;
}
function consoleHead(state: "run" | "done", sim: boolean) {
  return `<div class="console-head">${state === "run" ? '<span class="spin"></span>' : '<span class="done">✓</span>'}<span>${state === "run" ? "Trackdown running" : "Trackdown complete"}</span>${sim ? '<span class="pill sim" style="height:18px;margin-left:6px">sim</span>' : ""}<button class="x" data-skip title="Skip (Esc)">${state === "run" ? "skip ›" : "×"}</button></div><div class="console-body"></div>`;
}
consoleEl.addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest("[data-skip]")) {
    if (tracing) skipFlag = true;
    else hideConsole();
  }
});

async function typeLine(body: HTMLElement, tool: keyof typeof TOOL, text: string, run: number) {
  const row = document.createElement("div");
  row.className = "step active";
  row.innerHTML = `<span class="tool">${toolChip(tool)}</span><span class="txt"></span><span class="res"></span>`;
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
  consoleEl.innerHTML = consoleHead("run", c.synthetic);
  consoleEl.classList.remove("hidden");
  const body = $(".console-body", consoleEl);
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
      g.ring(c.submittedId, "#ffffff", 260, 1100);
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
      row.querySelector(".res")!.innerHTML = `<b>+${newIds.length}</b>${step.rejected ? ` · ${step.rejected} ✕` : ""}`;
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
    g.ring(c.originId, ROLE.origin.color, 520, 1700);
    await typeLine(body, "detector", `Earliest known copy: ${fmtShort(c.nodes.find((n) => n.id === c.originId)!.t)} · ${Math.max(0, subPath.length - 1)} hops back`, run);
    await pause(1500);
    g.setHighlight(null);
  }
  // then the full spread pours in, in time order
  if (alive()) {
    const rest = c.nodes.filter((n) => !g.revealed?.has(n.id)).sort((a, b) => a.t - b.t);
    if (rest.length) {
      await typeLine(body, "detector", `Mapping the rest of the spread: ${rest.length} more posts`, run);
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
  consoleEl.innerHTML = consoleHead("done", c.synthetic);
  const b2 = $(".console-body", consoleEl);
  const hops = Math.max(0, subPath.length - 1);
  b2.innerHTML = `<div class="step"><span class="tool">${toolChip("gemini")}</span><span class="txt">${STATUS[c.status].word} · ${hops} hops · ${c.nodes.length} posts</span><span class="res"></span></div>`;
  g.fit(undefined, 1100);
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
    <h2>Shill <i>or</i> chill?</h2>
    <div class="sub">Drop a post, a link or a handle. We walk it back to the earliest copy we can find, then check how it spread.</div>
    <div class="drop"><textarea placeholder="Paste a post, a bsky.app link or @handle…" autofocus></textarea>
      <div class="row">${icon.image.replace("<svg", '<svg width="14" height="14"')}<span>Drop a screenshot here (Nemotron reads it)</span><span class="file"></span></div></div>
    <div class="examples">${EXAMPLES.map((e) => `<button data-ex="${esc(e)}">${esc(e.length > 46 ? e.slice(0, 44) + "…" : e)}</button>`).join("")}</div>
    <div class="foot"><div class="fine">Asks the live API first. If it isn't reachable, you get a clearly labelled <b>simulated</b> trackdown.</div><button class="btn" data-x>Cancel</button><button class="btn primary" data-go>${icon.search.replace("<svg", '<svg width="15" height="15"')} Trace it</button></div>
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
      $(".file", drop).innerHTML = `<span class="att">${icon.image.replace("<svg", '<svg width="12" height="12"')} ${esc(f.name.slice(0, 28))}</span>`;
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
    btn.innerHTML = `<span class="spin" style="width:13px;height:13px;border-radius:50%;border:2px solid rgba(0,0,0,.2);border-top-color:#000;animation:spin .8s linear infinite"></span> Asking the API…`;
    let c = await analyze(text);
    if (!c) {
      c = simulateFromInput(text);
      toast("Live API not reachable: running a simulated trackdown");
    }
    userCases.unshift(c);
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
else
  document.fonts.ready.then(() => {
    // first visit: run the full trackdown so the motion explains the product
    runTrace(first);
  });
listLive().then((rows) => {
  liveRows = rows;
  if (rows.length) renderRail();
});
