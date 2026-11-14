// Right-hand window: tabs for the verdict and the selected folio.
import { hasVerdict, modelTool, readingsDisagree, readingsOf, type LookupAssessment } from "../data/lookup";
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, httpUrl, isLineageEdge, roleOf, STATUS, TOOL } from "./format";
import { sharedTextHtml } from "./sharedText";
import { PLATFORM } from "./card";
import { cropHtml } from "./crop";
import { icon } from "./icons";
import { coordinationNote, factualFindings } from "./findings";
import { mountVariations } from "./variations";
import { hasStory } from "./story";

const LEAN: Record<string, string> = { organic_looking: "looks organic", copy_campaign_looking: "looks like a copy campaign", unclear: "unclear" };
const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);
type Tab = "verdict" | "folio";

export class Panel {
  el: HTMLElement;
  g: Graph;
  c: TraceCase | null = null;
  pending = false;
  tab: Tab = "verdict";
  private shown = ""; // case + tab + pending of the last render: re-rendering the same view skips the entry animation
  onReplay: () => void = () => {};
  onCloseSheet: () => void = () => {};
  constructor(el: HTMLElement, g: Graph) {
    this.el = el;
    this.g = g;
    el.addEventListener("click", (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>("[data-go],[data-act],[data-tab]");
      if (!t) return;
      e.preventDefault();
      if (t.dataset.tab) {
        this.tab = t.dataset.tab as Tab;
        this.render();
        this.el.querySelector<HTMLElement>(`[data-tab="${this.tab}"]`)?.focus();
      }
      if (t.dataset.go) {
        this.g.select(t.dataset.go);
        this.g.focus(t.dataset.go, Math.max(this.g.k, 0.95));
        this.el.querySelector<HTMLElement>('[data-tab="folio"]')?.focus(); // the panel re-rendered: keep focus in it
      }
      if (t.dataset.act === "close") {
        this.tab = "verdict";
        this.g.select(null);
        this.el.querySelector<HTMLElement>('[data-tab="verdict"]')?.focus(); // keep keyboard position
      }
      if (t.dataset.act === "replay") this.onReplay();
      if (t.dataset.act === "sheet-close") this.onCloseSheet();
      if (t.dataset.act === "path" && this.g.selId) this.g.setHighlight(new Set(this.g.pathToOrigin(this.g.selId)));
    });
    // tab list: arrows, Home and End move between tabs (roving focus)
    el.addEventListener("keydown", (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>("[role=tab]");
      if (!t) return;
      const tabs = [...el.querySelectorAll<HTMLElement>("[role=tab]")];
      const i = tabs.indexOf(t);
      const j = e.key === "ArrowRight" ? (i + 1) % tabs.length : e.key === "ArrowLeft" ? (i - 1 + tabs.length) % tabs.length : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : -1;
      if (j < 0) return;
      e.preventDefault();
      e.stopPropagation();
      this.tab = tabs[j].dataset.tab as Tab;
      this.render();
      this.el.querySelector<HTMLElement>(`[data-tab="${this.tab}"]`)?.focus();
    });
    el.addEventListener("mouseover", (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>("[data-go]");
      this.g.setHighlight(t ? this.g.lineage(t.dataset.go!) : this.g.selId ? this.g.lineage(this.g.selId) : null);
    });
  }

  set(c: TraceCase, pending: boolean) {
    this.c = c;
    this.pending = pending;
    if (!this.g.selId && this.tab === "folio") this.tab = "verdict";
    this.render();
  }

  showFolio(on: boolean) {
    this.tab = on ? "folio" : this.tab === "folio" ? "verdict" : this.tab;
    this.render();
  }

  render() {
    const c = this.c;
    if (!c) return;
    const sel = this.g.selId ? c.nodes.find((n) => n.id === this.g.selId) : null;
    if (this.tab === "folio" && !sel) this.tab = "verdict";
    const tabs = [
      ["verdict", c.lookup ? "Findings" : "Verdict", ""],
      ...(sel ? [["folio", "Post", ""]] : []),
    ] as [Tab, string, string][];
    const view = `${c.id}|${this.tab}|${this.pending}|${this.tab === "folio" ? sel?.id : ""}`;
    const quiet = view === this.shown;
    this.shown = view;
    const width = `${Math.round(c.confidence * 100)}%`;
    const body = this.pending && this.tab === "verdict" ? this.pendingHtml() : this.tab === "verdict" ? this.verdict(c) : this.detail(sel!);
    this.el.innerHTML = `<div class="panel-inner${quiet ? " quiet still" : ""}"><div class="win-head"><div class="tabs" role="tablist" aria-label="Result">${tabs
      .map(([id, label, n]) => `<button role="tab" id="tab-${id}" aria-controls="tabpanel" data-tab="${id}" aria-selected="${this.tab === id}" tabindex="${this.tab === id ? 0 : -1}" class="${this.tab === id ? "on" : ""}">${label}${n ? `<span class="n">${n}</span>` : ""}</button>`)
      .join("")}</div><button class="sheet-close mobile-only" data-act="sheet-close" aria-label="Close">${sm(icon.close, 14)}</button></div><div class="pane" role="tabpanel" id="tabpanel" aria-labelledby="tab-${this.tab}">${body}</div></div>`;
    mountVariations(this.el, c, this.tab);
    const bar = this.el.querySelector<HTMLElement>(".conf .bar i");
    if (bar && quiet) bar.style.width = width; // no new fill animation on a live update
    else if (bar) requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = width)));
  }

  coinTarget(): HTMLElement | null {
    return this.el.querySelector(".vcoin");
  }

  private pendingHtml() {
    const failed = this.c?.lookup?.state === "failed";
    if (this.c?.lookup) return `<div class="label">${this.c.mock ? "Mock · fictional data · " : ""}${failed ? "Lookup incomplete" : "Collecting…"}</div>${failed ? `<p class="lookup-message" role="alert">${esc(this.c.lookup.error || "The lookup failed. No verdict.")}</p>` : ""}<p class="lookup-message" role="status">${this.c.nodes.length} cards · ${this.c.receipts.length} receipts</p>`;
    return `<div class="pending"><div class="label">Tracing…</div></div>`;
  }

  private verdict(c: TraceCase) {
    const v = STATUS[c.status];
    const scored = c.scored !== false && !c.historical && c.status !== "insufficient";
    const missingConfidence = c.live && c.scored === false && c.status !== "insufficient";
    const unscored = c.historical ? "Not scored" : missingConfidence ? "Not recorded" : "No call";
    const unscoredNote = c.historical ? "Historical illustration, not a detector result." : missingConfidence ? "Detector confidence was not recorded for this verdict." : c.findingsOnly ? "Outside the window we monitor." : "The evidence doesn't settle it either way.";
    const notice = c.mock ? `<div class="notice"><b>Mock.</b> Fictional data; no live lookup.</div>` : c.synthetic
      ? `<div class="notice"><b>Simulated.</b> Fictional accounts; not a verdict on a real post.</div>`
      : c.historical
        ? `<div class="notice"><b>Historical record.</b> Written by hand, not a detector result.</div>`
        : "";
    const replay = hasChain(c) || c.lookup ? `<div class="replay"><button class="btn" data-act="replay">${sm(icon.play)} ${c.lookup ? "Replay" : "Replay"}</button></div>` : "";
    // a lookup answers first, in one sentence; the rest of the reading and the sources fold under it
    const [lead, rest] = leadSentence(c.summary);
    // a picture trace: its answer is the brief above (ui/variations.ts); here only the call and the sources
    if (hasStory(c)) return `
      <p class="meme-note">${esc(hasVerdict(c) ? `${STATUS[c.status].word}: ${STATUS[c.status].tag.toLowerCase()}, in a group we watch.` : "No coordination call.")}</p>
      <details class="more-sources"><summary>Sources</summary>${factualFindings(c.lookup)}</details>`;
    return `
      ${c.lookup && lead ? `<section class="answer" aria-label="What we found"><p class="answer-lead">${esc(lead)}</p>${rest ? `<details><summary>More</summary><p class="lookup-message">${esc(rest)}</p></details>` : ""}</section>` : ""}
      ${!hasVerdict(c) ? `<section class="verdict noverdict" aria-label="Coordination">
        <div class="label">No coordination call</div>
        <p class="vs">${esc(c.lookup ? coordinationNote(c.lookup) : c.summary)}</p>
        ${this.readings(c)}
        ${notice}
      </section>` : `<section class="verdict" style="--vc:${v.css}">
        <div class="vhead"><div class="vcoin" style="background-image:url(${coinImg(c.status)})" role="img" aria-label="${esc(v.word)} coin"></div><div><div class="vword${v.word.length > 8 ? " long" : ""}">${esc(v.word.toUpperCase())}</div><div class="vtag">${c.findingsOnly ? "Not monitored" : esc(c.tag ?? v.tag)}${c.live ? " · live" : ""}</div></div></div>
        <p class="vs">${esc(c.lookup ? coordinationNote(c.lookup) : c.summary)}</p>
        ${!scored && c.lookup ? "" : `<div class="conf${scored ? "" : " abstain"}"><div class="row"><span class="label">${c.lookup ? "Coordination confidence" : "Confidence"}</span><b>${scored ? Math.round(c.confidence * 100) + "%" : unscored}</b></div><div class="bar"><i></i></div>${scored ? "" : `<div class="conf-note">${unscoredNote}</div>`}</div>`}
        ${this.readings(c)}
        ${notice}
      </section>`}
      ${replay}
      ${c.lookup ? `<div class="sect label">Sources</div>${factualFindings(c.lookup)}` : ""}`;
  }

  /** model readings of the receipts. They are inferences: the status always comes from `status`. */
  private readings(c: TraceCase) {
    const lookup = c.lookup;
    if (!lookup) return "";
    const readings = readingsOf(lookup);
    const second = lookup.steps.find((step) => step.id === "second_reading");
    const waiting = second && (second.state === "running" || second.state === "pending") && readings.length < 2
      ? `<p class="receipt-meta">${esc(second.label || "Second reading")}: still running. It is added here when it arrives.</p>` : "";
    if (!readings.length) return waiting;
    const block = (r: LookupAssessment, i: number, lean: boolean) => {
      const tool = modelTool(r.model);
      return `<div class="ev t-${tool}"><div class="by t-${tool}">${i ? "Second reading" : "Reading"} · ${esc(r.model)}</div><p>${esc(r.summary)}</p><div class="receipt-meta">${lean ? `Model's lean: ${esc(LEAN[r.llm_reading] ?? String(r.llm_reading))} · a model inference, not the status` : "Agrees with the first reading · a model inference, not the status"}</div></div>`;
    };
    if (readings.length > 1 && readingsDisagree(lookup)) return `<div class="notice">The two readings differ. Both are shown; neither sets the status.</div>${readings.map((r, i) => block(r, i, true)).join("")}${waiting}`;
    // the primary reading's summary is the lead paragraph above
    return `${readings.slice(1).map((r, i) => block(r, i + 1, false)).join("")}${waiting}`;
  }

  private realData(node: TraceNode) {
    if (!node.realData) return "";
    return `<div class="sect label">${this.c?.mock ? "Mock data (fictional)" : "Real data"}</div>${node.realData.map((group) => `<section class="data-group"><h4>${esc(group.label)}</h4><dl>${group.facts.map((fact) => `<dt>${esc(fact.label)}</dt><dd>${esc(fact.value)}</dd>`).join("")}</dl><div class="receipt-meta">${group.receiptId != null ? `Receipt #${group.receiptId}` : "No receipt id returned"}</div>${httpUrl(group.url) ? `<a class="src" href="${esc(group.url!)}" target="_blank" rel="noopener noreferrer">Source ↗ ${esc(group.url!)}</a>` : ""}</section>`).join("")}${node.timeUnknown ? '<p class="lookup-message">Time unknown: this card is positioned at the submitted post’s time (or the lookup start) for layout only. This is not a publication timestamp.</p>' : ""}`;
  }

  private detail(n: TraceNode) {
    const c = this.c!;
    const nodes = new Map(c.nodes.map((x) => [x.id, x]));
    // copy/spread links drive "earlier / later" and the path; a screenshot link is shown on its own
    const inE = c.edges.filter((e) => e.to === n.id && isLineageEdge(e, nodes.get(e.from), n));
    const outE = c.edges.filter((e) => e.from === n.id && isLineageEdge(e, n, nodes.get(e.to)));
    const shotOf = c.edges.filter((e) => e.from === n.id && e.rel === "screenshot_of" && nodes.has(e.to));
    const shownIn = c.edges.filter((e) => e.to === n.id && e.rel === "screenshot_of" && nodes.has(e.from));
    const a = n.author;
    const fresh = (a.ageDays ?? 999) < 30;
    // a screenshot link describes the screenshot, never a person
    const shotLine = (e: TraceCase["edges"][number], peer: TraceNode, text: string) => `<div><b>${text}</b> <a href="#" data-go="${esc(peer.id)}">${esc(peer.author.name)}</a>${e.sim != null ? ` · word similarity ${e.sim.toFixed(2)}` : ""}${e.tool ? ` <span class="rel-by">· ${esc(TOOL[e.tool]?.long ?? e.tool)}</span>` : ""}.${e.note ? ` <span class="rel-note">${esc(e.note)}.</span>` : ""}</div>`;
    const shots = [
      ...shotOf.map((e) => shotLine(e, nodes.get(e.to)!, "A screenshot in this post shows a post by")),
      ...shownIn.map((e) => shotLine(e, nodes.get(e.from)!, "Shown in a screenshot in a post by")),
    ].join("");
    return `<section class="detail">
      <button class="close" data-act="close" aria-label="Close">${sm(icon.close, 13)}</button>
      <div class="label" style="color:${roleOf(n.role).color}">${esc(roleOf(n.role).label)}</div>
      <div class="d-who">${cropHtml(a.handle, { templated: a.templated, blank: fresh && !a.templated, size: 42 })}<div class="who"><b>${esc(a.name)}</b>${a.handle !== a.name ? `<span>${esc(a.handle)}</span>` : ""}</div></div>
      <p class="d-txt">${sharedTextHtml(n, c)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        ${n.undated ? "" : `<div><div class="k2">Posted</div><div class="v2">${n.timeUnknown ? "Time unknown" : fmtDate(n.t)}</div></div>`}
        <div><div class="k2">Where</div><div class="v2">${esc(PLATFORM[n.platform] ?? n.platform)}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${this.realData(n)}
      ${shots ? `<div class="rel">${shots}</div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="btn" data-go="${esc(inE[0].from)}">${sm(icon.arrowL)} Earlier post</button>` : ""}
        ${outE[0] ? `<button class="btn" data-go="${esc(outE[0].to)}">Later post ${sm(icon.arrowR)}</button>` : ""}
        ${inE.length ? `<button class="btn" data-act="path">Show path to earliest</button>` : ""}
        ${shotOf[0] ? `<button class="btn" data-go="${esc(shotOf[0].to)}">Real post in the screenshot ${sm(icon.arrowR)}</button>` : ""}
        ${shownIn[0] ? `<button class="btn" data-go="${esc(shownIn[0].from)}">${sm(icon.arrowL)} Post with the screenshot</button>` : ""}
      </div>
      ${c.synthetic ? `<div class="nolink">Simulated post: no live link.</div>` : httpUrl(n.url) ? `<a class="d-src" href="${esc(n.url!)}" target="_blank" rel="noopener">Open the original ↗</a>` : ""}
    </section>`;
  }
}

/** the first sentence of a model summary, and the rest: the panel leads with one sentence */
export function leadSentence(text: string | null | undefined): [string, string] {
  const clean = (text ?? "").trim();
  const cut = /^(.{40,}?[.!?])\s+(\S.*)$/s.exec(clean);
  return cut ? [cut[1], cut[2]] : [clean, ""];
}

/** a post in the copy/spread trace: not a search result, not the real post shown in a screenshot */
const inTrace = (n: TraceNode) => n.role !== "source" && n.role !== "screenshotted";
/** a drawable chain: at least two posts in the trace */
export const hasChain = (c: TraceCase) => c.nodes.filter((n) => inTrace(n) && (!n.imageMatch || n.id === c.originId)).length > 1; // an image match is in the chain only as its dated origin
