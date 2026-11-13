// Right-hand window: tabs for the verdict, the provenance chain, the receipts and the selected folio.
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, REL, ROLE, STATUS, TOOL } from "./format";
import { arms, armsSeed } from "./heraldry";
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
    const pro = c.receipts.filter((r) => !r.against).length;
    const path = this.g.pathToOrigin(c.submittedId).length;
    const tabs = [
      ["verdict", "Verdict", ""],
      ["provenance", "Provenance", String(path)],
      ["receipts", "Receipts", String(c.receipts.length || pro)],
      ...(sel ? [["folio", "Folio", ""]] : []),
    ] as [Tab, string, string][];
    const body = this.pending ? this.pendingHtml() : this.tab === "verdict" ? this.verdict(c) : this.tab === "provenance" ? this.provenance(c) : this.tab === "receipts" ? this.receipts(c) : this.detail(sel!);
    this.el.innerHTML = `<div class="panel-inner"><div class="win-head"><div class="win-title">Attribution</div><div class="tabs">${tabs
      .map(([id, label, n]) => `<button data-tab="${id}" class="${this.tab === id ? "on" : ""}">${label}${n ? `<span class="n">${n}</span>` : ""}</button>`)
      .join("")}</div></div><div class="pane">${body}</div></div>`;
    const segs = this.el.querySelectorAll<HTMLElement>(".segbar .segs i");
    const on = Math.round(c.confidence * 20);
    segs.forEach((s, i) => setTimeout(() => s.classList.toggle("on", i < on && !this.el.querySelector(".segbar.abstain")), 30 + i * 35));
  }

  coinTarget(): HTMLElement | null {
    return this.el.querySelector(".vcoin");
  }

  private pendingHtml() {
    return `<section class="verdict gilt hud-corners" style="--vc:var(--cv)"><div class="vk"><span class="term-head" style="padding:0;border:0"><span class="rec"></span></span>Analysis in progress</div>
      <div class="vw long" style="color:var(--cv)">Tracing…</div>
      <p class="vs" style="color:var(--ink-2)">Walking back one copy at a time to the earliest version we can find, then measuring how it spread.</p>
      <div class="segbar"><div class="segs">${"<i></i>".repeat(20)}</div></div></section>`;
  }

  private verdict(c: TraceCase) {
    const v = STATUS[c.status];
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    const sub = nodes.get(c.submittedId);
    const ori = nodes.get(c.originId);
    const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
    const fresh = c.nodes.filter((n) => (n.author.ageDays ?? 999) < 30).length;
    const hops = Math.max(0, this.g.pathToOrigin(c.submittedId).length - 1);
    const scored = c.scored !== false && c.status !== "insufficient";
    const notice = c.synthetic
      ? `<div class="notice"><span><b>Simulated case.</b> Fictional accounts and <span class="mono" style="font-size:11px">.example</span> sites, built to show how a trackdown works. Not a verdict on any real post.</span></div>`
      : c.historical
        ? `<div class="notice"><span><b>Historical record.</b> Written by hand from public history, not run through the detector. Same question: wrong, or coordinated?</span></div>`
        : "";
    const tipConf = scored
      ? `<h4>Confidence</h4><div class='row'><span>Detector + analyst</span><b>${Math.round(c.confidence * 100)}%</b></div><hr><div class='row'><span>Receipts for</span><b>${c.receipts.filter((r) => !r.against).length}</b></div><div class='row'><span>Counter-evidence</span><b class='neg'>${c.receipts.filter((r) => r.against).length}</b></div>`
      : `<h4>No score</h4>${c.status === "insufficient" ? "Too little evidence to test coordination. We abstain instead of guessing." : "Historical case: described, not scored."}`;
    return `
      <section class="verdict gilt hud-corners" style="--vc:${v.css}">
        <div class="vcoin" style="background-image:url(${coinImg(c.status)})"></div>
        <div class="vk">Verdict${c.live ? ' <span class="flagtag live">LIVE</span>' : ""}</div>
        <div class="vw${v.word.length > 8 ? " long" : ""}">${v.word}</div>
        <div class="vtag">${v.tag}</div>
        <p class="vs">${esc(c.summary)}</p>
        <div class="segbar${scored ? "" : " abstain"}" data-tip="${esc(tipConf)}"><div class="row"><span>Confidence</span><b>${scored ? Math.round(c.confidence * 100) + "%" : "Abstain"}</b></div><div class="segs">${"<i></i>".repeat(20)}</div></div>
        ${notice}
      </section>
      <div class="stats">
        <div class="stat" data-tip="${esc(`<h4>Copies back</h4>Hops from the submitted post to the earliest copy we found.`)}"><div class="n">${hops}</div><div class="l">Copies back</div></div>
        <div class="stat" data-tip="${esc(`<h4>Age of the claim</h4><div class='row'><span>Earliest</span><b>${ori ? fmtDate(ori.t, false) : "–"}</b></div><div class='row'><span>Submitted</span><b>${sub ? fmtDate(sub.t, false) : "–"}</b></div>`)}"><div class="n">${sub && ori ? fmtDur(sub.t - ori.t) : "–"}</div><div class="l">To origin</div></div>
        <div class="stat" data-tip="${esc(`<h4>${c.historical ? "Hands" : "Accounts"}</h4><div class='row'><span>Distinct</span><b>${accounts}</b></div><div class='row'><span>Under 30 days old</span><b class='${fresh ? "neg" : ""}'>${fresh}</b></div>`)}"><div class="n">${accounts}</div><div class="l">${c.historical ? "Hands" : "Accounts"}</div></div>
      </div>
      <div class="replay"><button class="btn" data-act="replay">${sm(icon.play)} Replay the trackdown</button></div>
      <p class="colophon">We judge behaviour (timing, copies, account age), never people or opinions. “Shill” names a cluster's spread, not any account in it.</p>`;
  }

  private provenance(c: TraceCase) {
    const path = this.g.pathToOrigin(c.submittedId).reverse();
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    return `<div class="sect">Chain of copies <span class="n">${path.length}</span></div>${path
      .map((id, i) => {
        const n = nodes.get(id)!;
        const prev = i ? nodes.get(path[i - 1]) : null;
        const e = prev ? c.edges.find((x) => x.from === prev.id && x.to === id) : null;
        return `<button class="hop" data-go="${id}" style="--c:${ROLE[n.role].color}"><span class="rn">${i + 1}</span><span style="min-width:0"><div class="h-t">${esc(n.author.name)}</div><div class="h-s">${esc(ROLE[n.role].label)} · ${fmtDate(n.t, false)}</div></span><span class="h-gap">${prev ? `<b>+${fmtDur(n.t - prev.t)}</b>${e ? REL[e.rel].label : ""}${e?.sim != null ? ` · ${e.sim.toFixed(2)}` : ""}` : "<b>Origin</b>"}</span></button>`;
      })
      .join("")}
      ${c.clusters.length ? `<div class="sect">Territories <span class="n">${c.clusters.length}</span></div>${c.clusters.map((cl) => `<div class="ev" style="--tc:${STATUS[cl.status].color}"><div class="by">${STATUS[cl.status].short} · ${esc(cl.label)}</div><p>${esc(cl.stat)}</p></div>`).join("")}` : ""}`;
  }

  private receipts(c: TraceCase) {
    const link = (url: string) => (c.synthetic ? `<div class="nolink">${esc(url)} · simulated, no live link</div>` : `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`);
    const ev = (r: (typeof c.receipts)[0], i: number) => `<div class="ev t-${r.tool}${r.against ? " contra" : ""}" style="animation-delay:${i * 45}ms"><div class="by t-${r.tool}">${esc(TOOL[r.tool].long)}</div><p>${esc(r.text)}</p>${r.url ? link(r.url) : ""}</div>`;
    const pro = c.receipts.filter((r) => !r.against);
    const con = c.receipts.filter((r) => r.against);
    return `<div class="sect">Evidence <span class="n">${pro.length}</span></div>${pro.map(ev).join("")}
      ${con.length ? `<div class="sect">Why it might be chill <span class="n">${con.length}</span></div>${con.map((r, i) => ev(r, i + pro.length)).join("")}` : ""}
      ${c.wouldChange.length ? `<div class="sect">What would change our mind</div>${c.wouldChange.map((w) => `<div class="change">${esc(w)}</div>`).join("")}` : ""}`;
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
        return `<div><b>${REL[e.rel].label[0].toUpperCase() + REL[e.rel].label.slice(1)}${e.sim != null ? ` · match ${e.sim.toFixed(4)}` : ""}</b> from <a href="#" data-go="${p.id}">${esc(p.author.name)}</a>, ${fmtDur(n.t - p.t)} later${e.tool ? ` <span class="by t-${e.tool}" style="font:600 9.5px var(--hud);letter-spacing:.18em;text-transform:uppercase;color:var(--tc)">· ${TOOL[e.tool].label}</span>` : ""}.${e.note ? ` <span style="color:var(--ink-3)">${esc(e.note)}.</span>` : ""}</div>`;
      })
      .join("");
    return `<section class="detail gilt hud-corners">
      <button class="close" data-act="close" aria-label="Close">${sm(icon.close, 13)}</button>
      <div class="vk" style="font:600 10px var(--hud);letter-spacing:.2em;color:var(--ink-3);text-transform:uppercase">${esc(ROLE[n.role].label)}</div>
      <div style="display:flex;gap:12px;align-items:center;margin-top:10px">${arms(armsSeed(a.handle, a.templated), { blank: fresh && !a.templated, size: 40 })}<div class="who"><b style="font-size:18px">${esc(a.name)}</b><span>${esc(a.handle)}</span></div></div>
      <p style="margin:12px 0 0;font:400 15.5px/1.5 var(--body)">${esc(n.text)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        <div><div class="k2">Posted</div><div class="v2">${fmtDate(n.t)}</div></div>
        <div><div class="k2">Where</div><div class="v2" style="text-transform:capitalize">${n.platform}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${rel ? `<div class="rel">${rel}</div>` : n.role === "origin" ? `<div class="rel"><b>Earliest copy we found.</b> Nothing older matched; an older copy could still exist offline.</div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="btn" data-go="${inE[0].from}">${sm(icon.arrowL)} Parent</button>` : ""}
        ${outE[0] ? `<button class="btn" data-go="${outE[0].to}">Child ${sm(icon.arrowR)}</button>` : ""}
        <button class="btn" data-act="path">Path to origin</button>
      </div>
      ${n.url && !c.synthetic ? `<a class="src" style="display:inline-block;margin-top:10px;font:400 11px var(--mono);color:#8fb2f2" href="${esc(n.url)}" target="_blank" rel="noopener">Open the source ↗</a>` : `<div class="nolink">${c.synthetic ? "Simulated post: no live link." : ""}</div>`}
    </section>`;
  }
}
