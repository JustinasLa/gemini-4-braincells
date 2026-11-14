// Right-hand window: tabs for the verdict, the provenance chain, the receipts and the selected folio.
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, REL, ROLE, STATUS, TOOL } from "./format";
import { cropHtml } from "./crop";
import { icon } from "./icons";

const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);
type Tab = "verdict" | "provenance" | "receipts" | "folio";

export class Panel {
  el: HTMLElement;
  g: Graph;
  c: TraceCase | null = null;
  pending = false;
  tab: Tab = "verdict";
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
      }
      if (t.dataset.act === "close") {
        this.tab = "verdict";
        this.g.select(null);
      }
      if (t.dataset.act === "replay") this.onReplay();
      if (t.dataset.act === "sheet-close") this.onCloseSheet();
      if (t.dataset.act === "path" && this.g.selId) this.g.setHighlight(new Set(this.g.pathToOrigin(this.g.selId)));
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
      ["verdict", "Verdict", ""],
      ...(path > 1 ? [["provenance", "Chain", String(path)]] : []),
      ["receipts", "Evidence", String(c.receipts.length)],
      ...(sel ? [["folio", "Post", ""]] : []),
    ] as [Tab, string, string][];
    const body = this.pending ? this.pendingHtml() : this.tab === "verdict" ? this.verdict(c) : this.tab === "provenance" ? this.provenance(c) : this.tab === "receipts" ? this.receipts(c) : this.detail(sel!);
    this.el.innerHTML = `<div class="panel-inner"><div class="win-head"><div class="tabs" role="tablist" aria-label="Result">${tabs
      .map(([id, label, n]) => `<button role="tab" data-tab="${id}" aria-selected="${this.tab === id}" tabindex="${this.tab === id ? 0 : -1}" class="${this.tab === id ? "on" : ""}">${label}${n ? `<span class="n">${n}</span>` : ""}</button>`)
      .join("")}</div><button class="sheet-close mobile-only" data-act="sheet-close" aria-label="Close">${sm(icon.close, 14)}</button></div><div class="pane" role="tabpanel">${body}</div></div>`;
    const bar = this.el.querySelector<HTMLElement>(".conf .bar i");
    if (bar) requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = `${Math.round(c.confidence * 100)}%`)));
  }

  coinTarget(): HTMLElement | null {
    return this.el.querySelector(".vcoin");
  }

  private pendingHtml() {
    return `<div class="pending"><div class="label">Tracing</div>
      <p>Walking back one copy at a time to the earliest version we can find, then measuring how it spread.</p></div>`;
  }

  private verdict(c: TraceCase) {
    const v = STATUS[c.status];
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    const sub = nodes.get(c.submittedId);
    const ori = nodes.get(c.originId);
    const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
    const fresh = c.nodes.filter((n) => (n.author.ageDays ?? 999) < 30).length;
    const hops = Math.max(0, this.g.pathToOrigin(c.submittedId).length - 1);
    const scored = c.scored !== false && !c.historical && c.status !== "insufficient";
    const unscored = c.historical ? "Not scored" : "No call";
    const unscoredNote = c.historical ? "Historical illustration, not a detector result." : c.findingsOnly ? "Outside the window we monitor." : "The evidence doesn't settle it either way.";
    const notice = c.synthetic
      ? `<div class="notice"><b>Simulated case.</b> Fictional accounts and <span class="mono">.example</span> sites, built to show how a trackdown works. Not a verdict on any real post.</div>`
      : c.historical
        ? `<div class="notice"><b>Historical record.</b> Written by hand from public history, not run through the detector. The verdict is our reading of it.</div>`
        : "";
    const stats = hasChain(c)
      ? `<div class="stats">
        <div class="stat" data-tip="${esc(`<h4>Copies back</h4>Hops from the submitted post to the earliest copy we found.`)}"><div class="n">${hops}</div><div class="l">${hops === 1 ? "Copy back" : "Copies back"}</div></div>
        <div class="stat" data-tip="${esc(`<h4>Age of the claim</h4><div class='row'><span>Earliest</span><b>${ori ? fmtDate(ori.t, false) : "–"}</b></div><div class='row'><span>Submitted</span><b>${sub ? fmtDate(sub.t, false) : "–"}</b></div>`)}"><div class="n">${sub && ori ? fmtDur(sub.t - ori.t) : "–"}</div><div class="l">Since earliest</div></div>
        <div class="stat" data-tip="${esc(`<h4>${c.historical ? "Hands" : "Accounts"}</h4><div class='row'><span>Distinct</span><b>${accounts}</b></div><div class='row'><span>Under 30 days old</span><b class='${fresh ? "neg" : ""}'>${fresh}</b></div>`)}"><div class="n">${accounts}</div><div class="l">${c.historical ? "Hands" : accounts === 1 ? "Account" : "Accounts"}</div></div>
      </div>
      <div class="replay"><button class="btn" data-act="replay">${sm(icon.play)} Replay the trackdown</button></div>`
      : "";
    return `
      <section class="verdict" style="--vc:${v.css}">
        <div class="vhead"><div class="vcoin" style="background-image:url(${coinImg(c.status)})" role="img" aria-label="${esc(v.word)} coin"></div><div><div class="vword${v.word.length > 8 ? " long" : ""}">${esc(v.word.toUpperCase())}</div><div class="vtag">${v.tag}${c.live ? " · live" : ""}</div></div></div>
        <p class="vs">${esc(c.summary)}</p>
        <div class="conf${scored ? "" : " abstain"}"><div class="row"><span class="label">Confidence</span><b>${scored ? Math.round(c.confidence * 100) + "%" : unscored}</b></div><div class="bar"><i></i></div>${scored ? "" : `<div class="conf-note">${unscoredNote}</div>`}</div>
        ${notice}
      </section>
      ${stats}
      <p class="colophon">We judge behaviour (timing, copies, account age), never people or opinions. “Shill” names a cluster's spread, not any account in it.</p>`;
  }

  private provenance(c: TraceCase) {
    const path = this.g.pathToOrigin(c.submittedId).reverse();
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    return `<div class="sect label" style="margin-top:0">Chain of copies <span>${path.length}</span></div>${path
      .map((id, i) => {
        const n = nodes.get(id)!;
        const prev = i ? nodes.get(path[i - 1]) : null;
        const e = prev ? c.edges.find((x) => x.from === prev.id && x.to === id) : null;
        return `<button class="hop" data-go="${id}" style="--c:${ROLE[n.role].color}"><span class="rn">${i + 1}</span><span style="min-width:0"><div class="h-t">${esc(n.author.name)}</div><div class="h-s">${esc(ROLE[n.role].label)} · ${fmtDate(n.t, false)}</div></span><span class="h-gap">${prev ? `<b>+${fmtDur(n.t - prev.t)}</b>${e ? REL[e.rel].label : ""}${e?.sim != null ? ` · ${e.sim.toFixed(2)}` : ""}` : "<b>Earliest</b>"}</span></button>`;
      })
      .join("")}
      ${c.clusters.length ? `<div class="sect label">Detected clusters <span>${c.clusters.length}</span></div>${c.clusters.map((cl) => `<div class="ev" style="--tc:${STATUS[cl.status].color}"><div class="by">${STATUS[cl.status].short} · ${esc(cl.label)}</div><p>${esc(cl.stat)}</p></div>`).join("")}` : ""}`;
  }

  private receipts(c: TraceCase) {
    const short = (url: string) => {
      try {
        const u = new URL(url);
        return `${u.hostname.replace(/^www\./, "")}${u.pathname.length > 1 ? " · " + (u.pathname.split("/").filter(Boolean).pop() ?? "").slice(0, 32) : ""}`;
      } catch {
        return url.slice(0, 48);
      }
    };
    const link = (url: string) => (c.synthetic ? `<div class="nolink">${esc(short(url))} · simulated, no live link</div>` : `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(short(url))} ↗</a>`);
    const ev = (r: (typeof c.receipts)[0], i: number) => `<div class="ev t-${r.tool}" style="animation-delay:${i * 45}ms"><div class="by t-${r.tool}">${esc(TOOL[r.tool].long)}</div><p>${esc(r.text)}</p>${r.url ? link(r.url) : ""}</div>`;
    const pro = c.receipts.filter((r) => !r.against);
    const con = c.receipts.filter((r) => r.against);
    // `against` = points away from the verdict
    const conTitle = c.status === "likely_organic" ? "Signs of coordination" : "Signs of organic spread";
    const proTitle = c.findingsOnly ? "What we found" : "Supports the verdict";
    return `${pro.length ? `<div class="sect label" style="margin-top:0">${proTitle} <span>${pro.length}</span></div>${pro.map(ev).join("")}` : ""}
      ${con.length ? `<div class="sect label"${pro.length ? "" : ' style="margin-top:0"'}>${conTitle} <span>${con.length}</span></div>${con.map((r, i) => ev(r, i + pro.length)).join("")}` : ""}
      ${c.wouldChange.length ? `<div class="sect label">What would change our mind</div>${c.wouldChange.map((w) => `<div class="change">${esc(w)}</div>`).join("")}` : ""}`;
  }

  private detail(n: TraceNode) {
    const c = this.c!;
    const inE = c.edges.filter((e) => e.to === n.id);
    const outE = c.edges.filter((e) => e.from === n.id);
    const nodes = new Map(c.nodes.map((x) => [x.id, x]));
    const a = n.author;
    const fresh = (a.ageDays ?? 999) < 30;
    const rel = inE
      .map((e) => {
        const p = nodes.get(e.from)!;
        const when = n.undated || p.undated ? "" : `, ${fmtDur(n.t - p.t)} later`;
        return `<div><b>${REL[e.rel].label[0].toUpperCase() + REL[e.rel].label.slice(1)}${e.sim != null ? ` · match ${e.sim.toFixed(2)}` : ""}</b> from <a href="#" data-go="${p.id}">${esc(p.author.name)}</a>${when}${e.tool ? ` <span class="rel-by">· ${TOOL[e.tool].long}</span>` : ""}.${e.note ? ` <span class="rel-note">${esc(e.note)}.</span>` : ""}</div>`;
      })
      .join("");
    return `<section class="detail">
      <button class="close" data-act="close" aria-label="Close">${sm(icon.close, 13)}</button>
      <div class="label" style="color:${ROLE[n.role].color}">${esc(ROLE[n.role].label)}</div>
      <div class="d-who">${cropHtml(a.handle, { templated: a.templated, blank: fresh && !a.templated, size: 42 })}<div class="who"><b>${esc(a.name)}</b><span>${esc(a.handle)}</span></div></div>
      <p class="d-txt">${esc(n.text)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        ${n.undated ? "" : `<div><div class="k2">Posted</div><div class="v2">${fmtDate(n.t)}</div></div>`}
        <div><div class="k2">Where</div><div class="v2" style="text-transform:capitalize">${n.platform}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${rel ? `<div class="rel">${rel}</div>` : n.role === "origin" ? `<div class="rel"><b>Earliest copy we found.</b> Nothing older matched; an older copy could still exist offline.</div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="btn" data-go="${inE[0].from}">${sm(icon.arrowL)} Earlier post</button>` : ""}
        ${outE[0] ? `<button class="btn" data-go="${outE[0].to}">Later post ${sm(icon.arrowR)}</button>` : ""}
        ${inE.length ? `<button class="btn" data-act="path">Show path to earliest</button>` : ""}
      </div>
      ${n.url && !c.synthetic ? `<a class="d-src" href="${esc(n.url)}" target="_blank" rel="noopener">Open the original ↗</a>` : c.synthetic ? `<div class="nolink">Simulated post: no live link.</div>` : ""}
    </section>`;
  }
}

/** a drawable chain: at least two posts that aren't search results */
export const hasChain = (c: TraceCase) => c.nodes.filter((n) => n.role !== "source").length > 1;
