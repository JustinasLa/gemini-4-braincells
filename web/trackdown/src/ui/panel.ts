// Right-hand catalogue entry: attribution (verdict), provenance, evidence, the selected folio.
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, REL, ROLE, roman, STATUS, TOOL } from "./format";
import { icon } from "./icons";

const sm = (svg: string, n = 12) => svg.replace("<svg", `<svg width="${n}" height="${n}"`);

export class Panel {
  el: HTMLElement;
  g: Graph;
  c: TraceCase | null = null;
  pending = false;
  onReplay: () => void = () => {};
  constructor(el: HTMLElement, g: Graph) {
    this.el = el;
    this.g = g;
    el.addEventListener("click", (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>("[data-go],[data-act]");
      if (!t) return;
      e.preventDefault();
      if (t.dataset.go) {
        this.g.select(t.dataset.go);
        this.g.focus(t.dataset.go, Math.max(this.g.k, 0.95));
      }
      if (t.dataset.act === "close") this.g.select(null);
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
    this.render();
  }

  render() {
    const c = this.c;
    if (!c) return;
    const sel = this.g.selId ? c.nodes.find((n) => n.id === this.g.selId) : null;
    this.el.innerHTML = `<div class="panel-inner">${sel ? this.detail(sel) : ""}${this.pending ? this.pendingHtml() : this.overview(c)}</div>`;
    const bar = this.el.querySelector<HTMLElement>(".scale .bar i");
    if (bar) requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = `${Math.round(c.confidence * 100)}%`)));
  }

  coinTarget(): HTMLElement | null {
    return this.el.querySelector(".vcoin");
  }

  private pendingHtml() {
    return `<div class="verdict" style="--vc:var(--ink-3)"><div class="entry-label">Attribution pending</div>
      <div class="vw" style="font-size:52px;color:var(--ink-2)">Examining the receipts…</div>
      <p class="vs" style="color:var(--ink-2)">We walk back one copy at a time to the earliest version we can find, then look at how it spread.</p></div>`;
  }

  private overview(c: TraceCase) {
    const v = STATUS[c.status];
    const path = this.g.pathToOrigin(c.submittedId).reverse();
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    const sub = nodes.get(c.submittedId);
    const ori = nodes.get(c.originId);
    const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
    const hops = Math.max(0, path.length - 1);
    const provenance = path
      .map((id, i) => {
        const n = nodes.get(id)!;
        const prev = i ? nodes.get(path[i - 1]) : null;
        const e = prev ? c.edges.find((x) => x.from === prev.id && x.to === id) : null;
        return `<button class="hop" data-go="${id}" style="--c:${ROLE[n.role].color}"><span class="rn">${roman(i + 1)}.</span><span style="min-width:0"><div class="h-t">${esc(n.author.name)}</div><div class="h-s">${esc(ROLE[n.role].label)}, ${fmtDate(n.t, false)}</div></span><span class="h-gap">${prev ? `<b>+${fmtDur(n.t - prev.t)}</b>${e ? REL[e.rel].label : ""}` : "<b>origin</b>&nbsp;"}</span></button>`;
      })
      .join("");
    const pro = c.receipts.filter((r) => !r.against);
    const con = c.receipts.filter((r) => r.against);
    const link = (url: string) =>
      c.synthetic ? `<div class="nolink">${esc(url)} · simulated, no live link</div>` : `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`;
    const ev = (r: (typeof c.receipts)[0], i: number) =>
      `<div class="ev${r.against ? " contra" : ""}" style="animation-delay:${i * 50}ms"><div class="by t-${r.tool}">${esc(TOOL[r.tool].long)}</div><p>${esc(r.text)}</p>${r.url ? link(r.url) : ""}</div>`;
    const scored = c.scored !== false && c.status !== "insufficient";
    const notice = c.synthetic
      ? `<div class="notice"><b>Simulated case.</b> Fictional accounts and <span class="mono" style="font-size:12px">.example</span> sites, built to show how a trackdown works. Not a verdict on any real post.</div>`
      : c.historical
        ? `<div class="notice"><b>Historical record.</b> Written by hand from public history, not run through the detector. The question it answers is the same: wrong, or coordinated?</div>`
        : "";
    return `
      <section class="verdict" style="--vc:${v.css}">
        <div class="vcoin" style="background-image:url(${coinImg(c.status)})"></div>
        <div class="entry-label">Attribution${c.live ? ' <span class="tag live" style="transform:none;color:var(--verdigris)">live</span>' : ""}</div>
        <div class="vw${v.word.length > 8 ? " long" : ""}">${v.word}.</div>
        <div class="vtag">${v.tag.toUpperCase()}</div>
        <p class="vs">${esc(c.summary)}</p>
        <div class="scale${scored ? "" : " abstain"}"><div class="row"><span>${scored ? "Confidence" : c.status === "insufficient" ? "Confidence" : "Not scored"}</span><b>${scored ? Math.round(c.confidence * 100) + "%" : "–"}</b></div><div class="bar"><i></i></div></div>
        ${notice}
      </section>
      <div class="stats">
        <div class="stat"><div class="n">${hops}</div><div class="l">copies back</div></div>
        <div class="stat"><div class="n">${sub && ori ? fmtDur(sub.t - ori.t) : "–"}</div><div class="l">to the original</div></div>
        <div class="stat"><div class="n">${accounts}</div><div class="l">${c.historical ? "hands" : "accounts"}</div></div>
      </div>
      <div class="label" style="margin-top:26px">Provenance</div>
      <div class="provenance">${provenance}</div>
      <div class="label">Receipts <span class="mono" style="color:var(--ink-4)">${pro.length}</span></div>
      ${pro.map(ev).join("")}
      ${con.length ? `<div class="label">Why it might be chill</div>${con.map((r, i) => ev(r, i + pro.length)).join("")}` : ""}
      ${c.wouldChange.length ? `<div class="label">What would change our mind</div>${c.wouldChange.map((w) => `<div class="change">${esc(w)}</div>`).join("")}` : ""}
      <div class="replay"><button class="pbtn" data-act="replay">${sm(icon.play)} Replay the trackdown</button></div>
      <p class="colophon">We judge behaviour (timing, copies, account age), never people or opinions. “Shill” names a cluster's spread, not any account in it.</p>`;
  }

  private detail(n: TraceNode) {
    const c = this.c!;
    const inE = c.edges.filter((e) => e.to === n.id);
    const outE = c.edges.filter((e) => e.from === n.id);
    const nodes = new Map(c.nodes.map((x) => [x.id, x]));
    const a = n.author;
    const rel = inE
      .map((e) => {
        const p = nodes.get(e.from)!;
        return `<div><b>${REL[e.rel].label[0].toUpperCase() + REL[e.rel].label.slice(1)}${e.sim != null ? `, ${Math.round(e.sim * 100)}% match` : ""}</b> from <a href="#" data-go="${p.id}">${esc(p.author.name)}</a>, ${fmtDur(n.t - p.t)} later${e.tool ? ` <span class="by t-${e.tool}" style="font:500 10px var(--mono);letter-spacing:.12em;text-transform:uppercase">· ${TOOL[e.tool].label}</span>` : ""}.${e.note ? ` <i style="color:var(--ink-3)">${esc(e.note)}.</i>` : ""}</div>`;
      })
      .join("");
    const fresh = (a.ageDays ?? 999) < 30;
    return `<section class="detail">
      <button class="close" data-act="close" aria-label="Close">${sm(icon.close, 15)}</button>
      <div class="entry-label">Folio · ${esc(ROLE[n.role].label)}</div>
      <div style="display:flex;gap:12px;align-items:center;margin-top:12px"><div class="ava${fresh ? " fresh" : ""}" style="--h:${a.hue};width:40px;height:40px;font-size:15px">${esc(a.name.slice(0, 1))}</div><div class="who"><b style="font-size:19px">${esc(a.name)}</b><span>${esc(a.handle)}</span></div></div>
      <p style="margin:12px 0 0;font:400 18px/1.45 var(--serif)">${esc(n.text)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        <div><div class="k2">Posted</div><div class="v2">${fmtDate(n.t)}</div></div>
        <div><div class="k2">Where</div><div class="v2" style="text-transform:capitalize">${n.platform}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${rel ? `<div class="rel">${rel}</div>` : n.role === "origin" ? `<div class="rel"><b>Earliest copy we found.</b> Nothing older matched; an older copy could still exist offline.</div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="pbtn" data-go="${inE[0].from}">${sm(icon.arrowL)} Parent</button>` : ""}
        ${outE[0] ? `<button class="pbtn" data-go="${outE[0].to}">Child ${sm(icon.arrowR)}</button>` : ""}
        <button class="pbtn" data-act="path">Path to origin</button>
      </div>
      ${n.url && !c.synthetic ? `<a class="src" style="display:inline-block;margin-top:10px;font:400 11px var(--mono);color:var(--lapis)" href="${esc(n.url)}" target="_blank" rel="noopener">Open the source ↗</a>` : `<div class="nolink">${c.synthetic ? "Simulated post: no live link." : ""}</div>`}
    </section>`;
  }
}
