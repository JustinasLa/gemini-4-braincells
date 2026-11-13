import type { Media, TraceNode } from "../types";
import { esc, fmtDate, fmtNum, initials, ROLE } from "./format";
import { icon } from "./icons";

function mediaHtml(m: Media): string {
  const mh = m.hue ?? 210;
  const note = m.note ? `<div class="media-note">${esc(m.note)}</div>` : "";
  switch (m.kind) {
    case "link":
      return `<div class="media link" style="--mh:${mh}"><div class="thumb"></div><div class="lk"><div class="dom">${esc(m.domain ?? "link")}</div><div class="lt">${esc(m.title)}</div></div></div>${note}`;
    case "screenshot": {
      const cropped = /crop/i.test(m.note ?? "") || m.domain === "screenshot";
      return `<div class="media shot${cropped ? " cropped" : ""}"><div class="mast"><span>${esc((m.domain ?? "").toUpperCase())}</span><span class="tag">SATIRE</span></div><div class="hl">${esc(m.title)}</div><div class="bars"></div></div>${note}`;
    }
    case "photo":
      return `<div class="media photo" style="--mh:${mh}"><span class="cap">${esc(m.title)}</span></div>${note}`;
    case "video":
      return `<div class="media video" style="--mh:${mh}"><div class="play"></div><span class="cap">${esc(m.title)}</span></div>${note}`;
  }
}

const flagClass = (f: string) => (/old|followers|templated|every|first seen/i.test(f) ? (/old|followers/i.test(f) ? " warn" : " warn") : /dropped|cropped|changed|satire/i.test(f) ? " amber" : "");

/** Full card or compact chip for a node. */
export function cardHtml(n: TraceNode, compact: boolean): string {
  const a = n.author;
  const fresh = (a.ageDays ?? 999) < 30;
  const square = n.platform === "news" || n.platform === "web" || n.platform === "factcheck";
  const ava = `<div class="ava${fresh ? " fresh" : ""}${square ? " square" : ""}" style="--h:${a.hue}">${initials(a.name)}</div>`;
  const plat = `<span class="plat" title="${n.platform}">${icon[n.platform] ?? icon.web}</span>`;
  if (compact) {
    return `<header>${ava}<div class="who"><b>${esc(a.name)}</b><span>@${esc(a.handle)}</span></div>${plat}</header>
      <div class="chip-meta">${a.ageDays != null ? `<span class="flag warn">${a.ageDays} d old</span>` : ""}${a.followers != null ? `<span>${a.followers} fol.</span>` : ""}<time>${fmtClockShort(n.t)}</time></div>`;
  }
  const badge = ROLE[n.role].badge ? `<div class="role-badge">${n.role === "origin" ? icon.crown : n.role === "submitted" ? icon.target : n.role === "source" ? icon.search : icon.shuffle}${ROLE[n.role].badge}</div>` : "";
  const flags = n.flags?.length ? `<div class="flags">${n.flags.map((f) => `<span class="flag${flagClass(f)}">${esc(f)}</span>`).join("")}</div>` : "";
  const eng = n.reposts != null || n.likes != null ? `<span class="eng">${icon.repost}${fmtNum(n.reposts)}</span><span class="eng">${icon.heart}${fmtNum(n.likes)}</span>` : "";
  return `<header>${ava}<div class="who"><b>${esc(a.name)}</b><span>${n.platform === "bluesky" ? "@" : ""}${esc(a.handle)}</span></div>${plat}</header>
    ${badge}
    <p class="txt">${esc(n.text)}</p>
    ${n.media ? mediaHtml(n.media) : ""}
    ${flags}
    <footer>${eng}<time>${fmtDate(n.t)}</time></footer>`;
}

function fmtClockShort(t: number) {
  const d = new Date(t);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}:${String(d.getUTCSeconds()).padStart(2, "0")}`;
}
