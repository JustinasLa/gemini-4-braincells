// Right-hand window: tabs for the verdict, the provenance chain, the receipts and the selected folio.
import { modelTool, readingsDisagree, readingsOf, type LookupAssessment } from "../data/lookup";
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, httpUrl, isLineageEdge, relOf, roleOf, STATUS, TOOL } from "./format";
import { PLATFORM } from "./card";
import { cropHtml } from "./crop";
import { icon } from "./icons";
import { coordinationNote, factualFindings } from "./findings";
import { mountVariations } from "./variations";

const LEAN: Record<string, string> = { organic_looking: "looks organic", copy_campaign_looking: "looks like a copy campaign", unclear: "unclear" };
const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);
type Tab = "verdict" | "provenance" | "receipts" | "folio";

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
    const path = hasChain(c) ? this.g.pathToOrigin(c.submittedId).length : 0;
    if (this.tab === "provenance" && path < 2) this.tab = "verdict";
    const tabs = [
      ["verdict", c.lookup ? "Findings" : "Verdict", ""],
      ...(path > 1 ? [["provenance", "Chain", String(path)]] : []),
      ["receipts", "Evidence", String(c.receipts.length)],
      ...(sel ? [["folio", "Post", ""]] : []),
    ] as [Tab, string, string][];
    const view = `${c.id}|${this.tab}|${this.pending}|${this.tab === "folio" ? sel?.id : ""}`;
    const quiet = view === this.shown;
    this.shown = view;
    const width = `${Math.round(c.confidence * 100)}%`;
    const body = this.pending && this.tab === "verdict" ? this.pendingHtml() : this.tab === "verdict" ? this.verdict(c) : this.tab === "provenance" ? this.provenance(c) : this.tab === "receipts" ? this.receipts(c) : this.detail(sel!);
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
    if (this.c?.lookup) return `<div class="label">${this.c.mock ? "MOCK · fictional data · " : ""}${failed ? "Lookup incomplete" : "Collecting live receipts"}</div><p class="lookup-message" ${failed ? 'role="alert"' : 'role="status"'}>${esc(failed ? this.c.lookup.error || "The lookup failed. No final verdict was issued." : "Posts, pages and receipts appear as they arrive. Coordination is not judged until the lookup finishes.")}</p><p class="lookup-message">${this.c.nodes.length} cards · ${this.c.receipts.length} receipts. Select a card for its data, or open Evidence.</p>${this.spread(this.c)}`;
    return `<div class="pending"><div class="label">Tracing</div>
      <p>Walking back one copy at a time to the earliest version we can find, then measuring how it spread.</p></div>`;
  }

  private verdict(c: TraceCase) {
    const v = STATUS[c.status];
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    const sub = nodes.get(c.submittedId);
    const ori = nodes.get(c.originId);
    const accounts = new Set(c.nodes.filter((n) => inTrace(n) && !n.imageMatch).map((n) => n.author.handle)).size;
    const fresh = c.nodes.filter((n) => (n.author.ageDays ?? 999) < 30).length;
    const hops = Math.max(0, this.g.pathToOrigin(c.submittedId).length - 1);
    const scored = c.scored !== false && !c.historical && c.status !== "insufficient";
    const unscored = c.historical ? "Not scored" : "No call";
    const unscoredNote = c.historical ? "Historical illustration, not a detector result." : c.lookup ? "Coordination only: no conclusive detector verdict. See factual findings separately." : c.findingsOnly ? "Outside the window we monitor." : "The evidence doesn't settle it either way.";
    const notice = c.mock ? `<div class="notice"><b>Development mock.</b> All people, metrics and <span class="mono">.example</span> sources here are fictional. No live lookup was made.</div>` : c.synthetic
      ? `<div class="notice"><b>Simulated case.</b> Fictional accounts and <span class="mono">.example</span> sites, built to show how a trackdown works. Not a verdict on any real post.</div>`
      : c.historical
        ? `<div class="notice"><b>Historical record.</b> Written by hand from public history, not run through the detector. The verdict is our reading of it.</div>`
        : "";
    const stats = hasChain(c)
      ? `<div class="stats">
        <div class="stat" data-tip="${esc(`<h4>Copies back</h4>Hops from the submitted post to the earliest copy we found.`)}"><div class="n">${hops}</div><div class="l">${hops === 1 ? "Copy back" : "Copies back"}</div></div>
        <div class="stat" data-tip="${esc(`<h4>Age of the claim</h4><div class='row'><span>Earliest</span><b>${ori ? fmtDate(ori.t, false) : "–"}</b></div><div class='row'><span>Submitted</span><b>${sub ? fmtDate(sub.t, false) : "–"}</b></div>`)}"><div class="n">${sub && ori ? fmtDur(sub.t - ori.t) : "–"}</div><div class="l">Earliest → submitted</div></div>
        <div class="stat" data-tip="${esc(`<h4>${c.historical ? "Hands" : "Accounts"}</h4><div class='row'><span>Distinct</span><b>${accounts}</b></div><div class='row'><span>Under 30 days old</span><b class='${fresh ? "neg" : ""}'>${fresh}</b></div>`)}"><div class="n">${accounts}</div><div class="l">${c.historical ? "Hands" : accounts === 1 ? "Account in trace" : "Accounts in trace"}</div></div>
      </div>`
      : "";
    const replay = hasChain(c) || c.lookup ? `<div class="replay"><button class="btn" data-act="replay">${sm(icon.play)} ${c.lookup ? "View collected cards" : "Replay the trackdown"}</button></div>` : "";
    return `
      ${c.lookup ? factualFindings(c.lookup) : ""}
      <section class="verdict" style="--vc:${v.css}">
        ${c.lookup ? '<div class="label">Coordination verdict · not a truth rating</div>' : ""}
        <div class="vhead"><div class="vcoin" style="background-image:url(${coinImg(c.status)})" role="img" aria-label="${esc(v.word)} coin"></div><div><div class="vword${v.word.length > 8 ? " long" : ""}">${esc(v.word.toUpperCase())}</div><div class="vtag">${c.findingsOnly ? "Not monitored" : esc(c.tag ?? v.tag)}${c.live ? " · live" : ""}</div></div></div>
        <p class="vs">${esc(c.lookup ? coordinationNote(c.lookup) : c.summary)}</p>
        <div class="conf${scored ? "" : " abstain"}"><div class="row"><span class="label">${c.lookup ? "Coordination confidence" : "Confidence"}</span><b>${scored ? Math.round(c.confidence * 100) + "%" : unscored}</b></div><div class="bar"><i></i></div>${scored ? "" : `<div class="conf-note">${unscoredNote}</div>`}</div>
        ${this.readings(c)}
        ${notice}
      </section>
      ${c.lookup ? `<div class="sect label">Model summary · collected evidence</div><p class="lookup-message">${esc(c.summary)}</p>` : ""}
      ${stats}
      ${replay}
      ${this.spread(c)}`;
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
    return `<div class="receipt-meta">Model reading by ${esc(readings[0].model)} · not a verdict</div>${readings.slice(1).map((r, i) => block(r, i + 1, false)).join("")}${waiting}`;
  }

  private provenance(c: TraceCase) {
    const path = this.g.pathToOrigin(c.submittedId).reverse();
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    return `<div class="sect label" style="margin-top:0">Chain of copies <span>${path.length}</span></div>${path
      .map((id, i) => {
        const n = nodes.get(id)!;
        const prev = i ? nodes.get(path[i - 1]) : null;
        const e = prev ? c.edges.find((x) => x.from === prev.id && x.to === id) : null;
        const start = c.lookup ? "Path start" : "Earliest";
        return `<button class="hop" data-go="${esc(id)}" style="--c:${roleOf(n.role).color}"><span class="rn">${i + 1}</span><span style="min-width:0"><div class="h-t">${esc(n.author.name)}</div><div class="h-s">${esc(roleOf(n.role).label)} · ${n.timeUnknown || n.undated ? "time unknown" : fmtDate(n.t, false)}</div></span><span class="h-gap">${prev ? `<b>${n.timeUnknown || prev.timeUnknown || n.undated || prev.undated ? "gap unknown" : `+${fmtDur(n.t - prev.t)}`}</b>${e ? esc(relOf(e.rel).label) : ""}${e?.sim != null ? ` · match ${e.sim.toFixed(2)}` : ""}` : `<b>${start}</b>`}</span></button>`;
      })
      .join("")}
      ${c.clusters.length ? `<div class="sect label">${c.lookup ? "Monitored clusters / copy samples" : "Detected clusters"} <span>${c.clusters.length}</span></div>${c.clusters.map((cl) => `<div class="ev" style="--tc:${STATUS[cl.status].color}"><div class="by">${STATUS[cl.status].short} · ${esc(cl.label)}</div><p>${esc(cl.stat)}</p></div>`).join("")}` : ""}`;
  }

  private receipts(c: TraceCase) {
    const short = (url: string) => {
      try {
        const u = new URL(url);
        if (/(^|\.)bsky\.app$/.test(u.hostname)) return u.pathname.includes("/post/") ? "Bluesky post" : "Bluesky profile";
        return `${u.hostname.replace(/^www\./, "")}${u.pathname.length > 1 ? " · " + (u.pathname.split("/").filter(Boolean).pop() ?? "").slice(0, 32) : ""}`;
      } catch {
        return url.slice(0, 48);
      }
    };
    const link = (url: string) => (c.synthetic ? `<div class="nolink">${esc(short(url))} · simulated, no live link</div>` : `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(short(url))} ↗</a>`);
    const ev = (r: (typeof c.receipts)[0], i: number) => `<div class="ev t-${r.tool}" ${r.id != null ? `id="receipt-${r.id}"` : ""} style="animation-delay:${i * 45}ms"><div class="by t-${r.tool}">${r.id != null ? `Receipt #${r.id} · ` : ""}${esc(TOOL[r.tool].long)}</div><p>${esc(r.text)}</p>${r.endpoint ? `<div class="receipt-meta">${esc(r.endpoint)}${r.latencyMs != null ? ` · ${r.latencyMs} ms` : ""}${r.requestId ? ` · request ${esc(r.requestId)}` : ""}</div>` : ""}${r.url ? link(r.url) : ""}</div>`;
    const pro = c.receipts.filter((r) => !r.against);
    const con = c.receipts.filter((r) => r.against);
    // `against` = points away from the verdict
    const conTitle = c.status === "likely_organic" ? "Signs of coordination" : "Signs of organic spread";
    // an abstention has nothing to "support": its evidence is what was weighed
    const proTitle = c.findingsOnly || c.lookup ? "What we found" : c.status === "insufficient" ? "Evidence considered" : "Supports the verdict";
    const selected = c.nodes.find((node) => node.id === this.g.selId);
    const readings = c.lookup ? readingsOf(c.lookup) : [];
    const cited = (r: LookupAssessment) => `<div class="sect label">Cited model reading${readings.length > 1 ? ` · ${esc(r.model)}` : ""} · not a verdict</div><p class="lookup-message">${esc([r.origin, r.corroboration, r.spread_pattern].filter(Boolean).join("\n"))}</p><div class="receipt-meta">${esc(r.model)} · ${(r.cites ?? []).map((id) => `#${id}`).join(", ")}</div>`;
    const head = `${selected ? this.realData(selected) : c.lookup ? '<p class="lookup-message">Select a card to inspect its real data and source receipts.</p>' : ""}${this.spread(c)}${readings.map(cited).join("")}`;
    const first = !head.trim();
    return `${head}${pro.length ? `<div class="sect label"${first ? ' style="margin-top:0"' : ""}>${proTitle} <span>${pro.length}</span></div>${pro.map(ev).join("")}` : ""}
      ${con.length ? `<div class="sect label"${pro.length || !first ? "" : ' style="margin-top:0"'}>${conTitle} <span>${con.length}</span></div>${con.map((r, i) => ev(r, i + pro.length)).join("")}` : ""}
      ${c.wouldChange.length ? `<div class="sect label">What would change our mind</div>${c.wouldChange.map((w) => `<div class="change">${esc(w)}</div>`).join("")}` : ""}`;
  }

  private realData(node: TraceNode) {
    if (!node.realData) return "";
    return `<div class="sect label">${this.c?.mock ? "Mock data (fictional)" : "Real data"}</div>${node.realData.map((group) => `<section class="data-group"><h4>${esc(group.label)}</h4><dl>${group.facts.map((fact) => `<dt>${esc(fact.label)}</dt><dd>${esc(fact.value)}</dd>`).join("")}</dl><div class="receipt-meta">${group.receiptId != null ? `Receipt #${group.receiptId}` : "No receipt id returned"}</div>${httpUrl(group.url) ? `<a class="src" href="${esc(group.url!)}" target="_blank" rel="noopener noreferrer">Source ↗ ${esc(group.url!)}</a>` : ""}</section>`).join("")}${node.timeUnknown ? '<p class="lookup-message">Time unknown: this card is positioned at the submitted post’s time (or the lookup start) for layout only. This is not a publication timestamp.</p>' : ""}`;
  }

  private spread(c: TraceCase) {
    if (!c.lookup) return "";
    const spread = c.lookup.spread;
    return `${spread ? `<div class="sect label">Spread · observed sample, not a verdict</div><section class="data-group"><dl><dt>Posts / distinct accounts</dt><dd>${spread.n_posts} / ${spread.n_accounts}</dd><dt>Same-text matches (similarity ≥ 0.8)</dt><dd>${spread.n_verbatim}</dd><dt>First / last (UTC)</dt><dd>${esc(spread.first_at ?? "Unknown")} / ${esc(spread.last_at ?? "Unknown")}</dd><dt>Time span</dt><dd>${spread.span_s == null ? "Unknown" : fmtDur(spread.span_s * 1000)}</dd><dt>Accounts in first hour</dt><dd>${spread.first_hour_accounts ?? "Unknown"}</dd><dt>Smallest inter-account copy gap</dt><dd>${spread.min_gap_s == null ? "Unknown" : `${spread.min_gap_s} s`}</dd></dl></section>` : ""}`;
  }

  private detail(n: TraceNode) {
    const c = this.c!;
    const nodes = new Map(c.nodes.map((x) => [x.id, x]));
    // copy/spread links drive "earlier / later" and the path; a screenshot link is shown on its own
    const inE = c.edges.filter((e) => e.to === n.id && isLineageEdge(e, nodes.get(e.from), n));
    const outE = c.edges.filter((e) => e.from === n.id && isLineageEdge(e, n, nodes.get(e.to)));
    const shotOf = c.edges.filter((e) => e.from === n.id && e.rel === "screenshot_of" && nodes.has(e.to));
    const shownIn = c.edges.filter((e) => e.to === n.id && e.rel === "screenshot_of" && nodes.has(e.from));
    const other = c.edges.filter((e) => e.to === n.id && !isLineageEdge(e, nodes.get(e.from), n) && e.rel !== "screenshot_of" && nodes.has(e.from));
    const a = n.author;
    const fresh = (a.ageDays ?? 999) < 30;
    const rel = inE
      .concat(other)
      .map((e) => {
        const p = nodes.get(e.from);
        if (!p) return "";
        const when = n.undated || p.undated || n.timeUnknown || p.timeUnknown ? "" : `, ${fmtDur(n.t - p.t)} apart`;
        const label = relOf(e.rel).label;
        return `<div><b>${esc(label[0].toUpperCase() + label.slice(1))}${e.sim != null ? ` · match ${e.sim.toFixed(2)}` : ""}</b> from <a href="#" data-go="${esc(p.id)}">${esc(p.author.name)}</a>${when}${e.tool ? ` <span class="rel-by">· ${esc(TOOL[e.tool]?.long ?? e.tool)}</span>` : ""}.${e.note ? ` <span class="rel-note">${esc(e.note)}.</span>` : ""}</div>`;
      })
      .join("");
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
      <p class="d-txt">${esc(n.text)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        ${n.undated ? "" : `<div><div class="k2">Posted</div><div class="v2">${n.timeUnknown ? "Time unknown" : fmtDate(n.t)}</div></div>`}
        <div><div class="k2">Where</div><div class="v2">${PLATFORM[n.platform] ?? n.platform}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${this.realData(n)}
      ${shots ? `<div class="rel">${shots}</div>` : ""}
      ${rel ? `<div class="rel">${rel}</div>` : n.role === "origin" ? `<div class="rel"><b>Earliest copy we found.</b> Nothing older matched; an older copy could still exist offline.</div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="btn" data-go="${esc(inE[0].from)}">${sm(icon.arrowL)} Earlier post</button>` : ""}
        ${outE[0] ? `<button class="btn" data-go="${esc(outE[0].to)}">Later post ${sm(icon.arrowR)}</button>` : ""}
        ${inE.length ? `<button class="btn" data-act="path">Show path to earliest</button>` : ""}
        ${shotOf[0] ? `<button class="btn" data-go="${esc(shotOf[0].to)}">Real post in the screenshot ${sm(icon.arrowR)}</button>` : ""}
        ${shownIn[0] ? `<button class="btn" data-go="${esc(shownIn[0].from)}">${sm(icon.arrowL)} Post with the screenshot</button>` : ""}
      </div>
      ${n.url && !c.synthetic ? `<a class="d-src" href="${esc(n.url)}" target="_blank" rel="noopener">Open the original ↗</a>` : c.synthetic ? `<div class="nolink">Simulated post: no live link.</div>` : ""}
    </section>`;
  }
}

/** a post in the copy/spread trace: not a search result, not the real post shown in a screenshot */
const inTrace = (n: TraceNode) => n.role !== "source" && n.role !== "screenshotted";
/** a drawable chain: at least two posts in the trace */
export const hasChain = (c: TraceCase) => c.nodes.filter((n) => inTrace(n) && (!n.imageMatch || n.id === c.originId)).length > 1; // an image match is in the chain only as its dated origin
