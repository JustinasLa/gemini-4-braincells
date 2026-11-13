// Right-hand "Receipts" panel: verdict, trail to the source, evidence, selected-node detail.
import type { Graph } from "../engine/graph";
import type { TraceCase, TraceNode } from "../types";
import { coinImg } from "./coin";
import { esc, fmtDate, fmtDur, fmtNum, REL, ROLE, STATUS, TOOL, toolChip } from "./format";
import { icon } from "./icons";

export class Panel {
  el: HTMLElement;
  g: Graph;
  c: TraceCase | null = null;
  pending = false; // trace still running: verdict hidden
  onReplay: () => void = () => {};
  constructor(el: HTMLElement, g: Graph) {
    this.el = el;
    this.g = g;
    el.addEventListener("click", (e) => {
      const t = (e.target as HTMLElement).closest<HTMLElement>("[data-go],[data-act]");
      if (!t) return;
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
    const bar = this.el.querySelector<HTMLElement>(".conf .bar i");
    if (bar) requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = `${Math.round(c.confidence * 100)}%`)));
  }

  coinTarget(): HTMLElement | null {
    return this.el.querySelector(".vcoin");
  }

  private pendingHtml() {
    return `<div class="verdict" style="--vc:var(--ink-3)"><div class="vk"><span class="console-head" style="padding:0;border:0"><span class="spin"></span></span>Tracing</div>
      <div class="vw" style="color:var(--ink-2)"><i>Checking the receipts…</i></div>
      <div class="vs">Walking back hop by hop to the earliest copy we can find, then measuring how it spread.</div></div>`;
  }

  private overview(c: TraceCase) {
    const v = STATUS[c.status];
    const g = this.g;
    const path = g.pathToOrigin(c.submittedId).reverse();
    const nodes = new Map(c.nodes.map((n) => [n.id, n]));
    const sub = nodes.get(c.submittedId);
    const ori = nodes.get(c.originId);
    const accounts = new Set(c.nodes.filter((n) => n.role !== "source").map((n) => n.author.handle)).size;
    const hops = Math.max(0, path.length - 1);
    const lineage = path
      .map((id, i) => {
        const n = nodes.get(id)!;
        const prev = i ? nodes.get(path[i - 1]) : null;
        const e = prev ? c.edges.find((x) => x.from === prev.id && x.to === id) : null;
        return `<button class="hop" data-go="${id}" style="--c:${ROLE[n.role].color}"><span class="dot"></span><span style="min-width:0"><div class="h-t">${esc(n.author.name)}</div><div class="h-s">${esc(ROLE[n.role].label)} · ${fmtDate(n.t, false)}</div></span><span class="h-gap">${prev ? `<b>+${fmtDur(n.t - prev.t)}</b>${e ? REL[e.rel].label : ""}` : "<b>start</b>origin"}</span></button>`;
      })
      .join("");
    const pro = c.receipts.filter((r) => !r.against);
    const con = c.receipts.filter((r) => r.against);
    const rc = (r: (typeof c.receipts)[0], i: number) =>
      `<div class="receipt${r.against ? " against" : ""}" style="animation-delay:${i * 60}ms">${toolChip(r.tool)}${r.against ? '<span class="ag">counter-evidence</span>' : ""}<p>${esc(r.text)}</p>${r.url ? (c.synthetic ? `<div class="nolink">${icon.link.replace("<svg", '<svg width="10" height="10"')} ${esc(r.url)} · simulated</div>` : `<a class="src" href="${esc(r.url)}" target="_blank" rel="noopener">${icon.ext.replace("<svg", '<svg width="11" height="11"')} ${esc(r.url)}</a>`) : ""}</div>`;
    return `
      <div class="verdict" style="--vc:${v.css}">
        <div class="vcoin" style="background-image:url(${coinImg(c.status)})"></div>
        <div class="vk">Verdict ${c.live ? '<span class="pill live"><span class="blip"></span>live</span>' : ""}</div>
        <div class="vw${v.word.length > 8 ? " long" : ""}"><i>${v.word}.</i></div>
        <div class="vs">${esc(c.summary)}</div>
        <div class="conf${c.status === "insufficient" ? " abstain" : ""}"><div class="row"><span>${c.status === "insufficient" ? "Abstained" : "Confidence"}</span><b>${c.status === "insufficient" ? "–" : Math.round(c.confidence * 100) + "%"}</b></div><div class="bar"><i></i></div></div>
        ${c.synthetic ? `<div class="sim-note"><span>◆</span><span><b>Simulated case.</b> Fictional accounts and sites, made to show how a trackdown works. Not a verdict on any real post.</span></div>` : ""}
      </div>
      <div class="stats">
        <div class="stat"><div class="n">${hops}</div><div class="l">hops back</div></div>
        <div class="stat"><div class="n">${sub && ori ? fmtDur(sub.t - ori.t) : "–"}</div><div class="l">to origin</div></div>
        <div class="stat"><div class="n">${accounts}</div><div class="l">accounts</div></div>
      </div>
      <h3>${icon.crown.replace("<svg", '<svg width="12" height="12"')} Trail to the source</h3>
      <div class="lineage">${lineage}</div>
      <h3>${icon.receipt.replace("<svg", '<svg width="12" height="12"')} Receipts <span class="count">${pro.length}</span></h3>
      ${pro.map(rc).join("")}
      ${con.length ? `<h3>Why it might be chill <span class="count">${con.length}</span></h3>${con.map((r, i) => rc(r, i + pro.length)).join("")}` : ""}
      ${c.wouldChange.length ? `<h3>What would change our mind</h3>${c.wouldChange.map((w) => `<div class="change">${esc(w)}</div>`).join("")}` : ""}
      <div class="navrow" style="margin-top:18px"><button class="btn" data-act="replay">${icon.play.replace("<svg", '<svg width="12" height="12"')} Replay the trackdown</button></div>
      <p class="rail-note">We flag <b>behaviour</b> (timing, copies, account age), never people or opinions. “Shill” is a label for a cluster's spread, not for any account in it.</p>`;
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
        return `<div><b>${REL[e.rel].label}${e.sim != null ? ` · ${Math.round(e.sim * 100)}% match` : ""}</b> from <a href="#" data-go="${p.id}" style="color:var(--ink)">${esc(p.author.name)}</a>, ${fmtDur(n.t - p.t)} later ${e.tool ? toolChip(e.tool) : ""}${e.note ? `<div style="color:var(--ink-3);font-size:12px;margin-top:3px">${esc(e.note)}</div>` : ""}</div>`;
      })
      .join("");
    const fresh = (a.ageDays ?? 999) < 30;
    return `<div class="detail" style="--rc:${ROLE[n.role].color};margin-bottom:14px">
      <button class="close" data-act="close" aria-label="Close">${icon.close.replace("<svg", '<svg width="14" height="14"')}</button>
      <div style="display:flex;gap:10px;align-items:center"><div class="ava${fresh ? " fresh" : ""}" style="--h:${a.hue};width:38px;height:38px">${esc(a.name.slice(0, 1))}</div><div class="who"><b style="font-size:15px">${esc(a.name)}</b><span>${esc(a.handle)}</span></div></div>
      <div class="role-badge" style="--rc:${ROLE[n.role].color}">${esc(ROLE[n.role].label)}</div>
      <p style="margin:10px 0 0;font-size:14px;line-height:1.5">${esc(n.text)}</p>
      ${n.media?.note ? `<div class="media-note">${esc(n.media.note)}</div>` : ""}
      <div class="kv">
        <div><div class="k2">Posted</div><div class="v2">${fmtDate(n.t)}</div></div>
        <div><div class="k2">Platform</div><div class="v2" style="text-transform:capitalize">${n.platform}</div></div>
        ${a.ageDays != null ? `<div><div class="k2">Account age</div><div class="v2${fresh ? " warn" : ""}">${fmtDur(a.ageDays * 86400000)}</div></div>` : ""}
        ${a.followers != null ? `<div><div class="k2">Followers</div><div class="v2${a.followers < 20 ? " warn" : ""}">${fmtNum(a.followers)}</div></div>` : ""}
      </div>
      ${rel ? `<div class="rel">${rel}</div>` : n.role === "origin" ? `<div class="rel"><div><b>Earliest copy we found.</b> Nothing older matched; an older copy could still exist offline.</div></div>` : ""}
      <div class="navrow">
        ${inE[0] ? `<button class="btn" data-go="${inE[0].from}">${icon.arrowL.replace("<svg", '<svg width="13" height="13"')} Parent</button>` : ""}
        ${outE[0] ? `<button class="btn" data-go="${outE[0].to}">Child ${icon.arrowR.replace("<svg", '<svg width="13" height="13"')}</button>` : ""}
        <button class="btn" data-act="path">${icon.crown.replace("<svg", '<svg width="13" height="13"')} Path to origin</button>
      </div>
      ${n.url && !c.synthetic ? `<a class="src" style="display:inline-flex;gap:6px;margin-top:10px;font:500 11px var(--mono);color:var(--source)" href="${esc(n.url)}" target="_blank" rel="noopener">${icon.ext.replace("<svg", '<svg width="11" height="11"')} Open source</a>` : `<div class="nolink">${c.synthetic ? "Simulated post: no live link." : ""}</div>`}
    </div>`;
  }
}

export { TOOL };
