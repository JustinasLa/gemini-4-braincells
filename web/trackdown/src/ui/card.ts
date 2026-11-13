import type { Media, TraceNode } from "../types";
import { esc, fmtDate, fmtNum, initials, ROLE } from "./format";
import { icon } from "./icons";

const PLATFORM: Record<string, string> = {
  bluesky: "Bluesky",
  web: "Web page",
  news: "News",
  forum: "Forum",
  video: "Video",
  chat: "Chat",
  factcheck: "Web page",
  print: "Print",
  letter: "Letter",
};

function mediaHtml(m: Media): string {
  const note = m.note ? `<div class="media-note">${esc(m.note)}</div>` : "";
  switch (m.kind) {
    case "link":
      return `<div class="media link"><div class="thumb"></div><div class="lk"><div class="dom">${esc(m.domain ?? "link")}</div><div class="lt">${esc(m.title)}</div></div></div>${note}`;
    case "screenshot": {
      const cropped = /crop/i.test(m.note ?? "") || m.domain === "screenshot";
      return `<div class="media shot${cropped ? " cropped" : ""}"><div class="mast"><span>${esc((m.domain ?? "").toUpperCase())}</span><span class="tag2">SATIRE</span></div><div class="hl">${esc(m.title)}</div><div class="bars"></div></div>${note}`;
    }
    case "photo":
      return `<div class="media photo"><span class="cap">${esc(m.title)}</span></div>${note}`;
    case "video":
      return `<div class="media video"><div class="play"></div><span class="cap">${esc(m.title)}</span></div>${note}`;
    case "plate":
      return `<div class="media plate"><img src="${m.src}" alt="${esc(m.title)}" draggable="false" loading="lazy"><span class="cap">${esc(m.title)}</span></div>${note}`;
  }
}

const flagClass = (f: string) => (/old|followers|templated|every|first seen|never saw|invented/i.test(f) ? " warn" : /dropped|cropped|changed|satire|altered/i.test(f) ? " amber" : "");

/** Folio (full card) or slip (compact cluster member). `no` is the catalogue number. */
export function cardHtml(n: TraceNode, compact: boolean, no: number): string {
  const a = n.author;
  const fresh = (a.ageDays ?? 999) < 30;
  const square = ["news", "web", "factcheck", "print", "letter"].includes(n.platform);
  const ava = `<div class="ava${fresh ? " fresh" : ""}${square ? " square" : ""}" style="--h:${a.hue}">${esc(initials(a.name))}</div>`;
  const handle = `${n.platform === "bluesky" ? "@" : ""}${esc(a.handle)}`;
  if (compact) {
    const t = new Date(n.t);
    const clock = `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}:${String(t.getUTCSeconds()).padStart(2, "0")}`;
    return `<header>${ava}<div class="who"><b>${esc(a.name)}</b><span>${handle}</span></div></header>
      <div class="chip-meta">${a.ageDays != null ? `<span class="age">${a.ageDays} d old</span>` : ""}${a.followers != null ? `<span>${a.followers} fol.</span>` : ""}<time>${clock}</time></div>`;
  }
  const mark = ROLE[n.role].mark ? `<div class="rolemark">${esc(ROLE[n.role].mark!)}</div>` : "";
  const stamp = n.role === "submitted" ? `<div class="stamp">Exhibit A</div>` : "";
  const flags = n.flags?.length ? `<div class="flags">${n.flags.map((f) => `<span class="flag${flagClass(f)}">${esc(f)}</span>`).join("")}</div>` : "";
  const eng = n.reposts != null || n.likes != null ? `<span class="eng">${icon.repost}${fmtNum(n.reposts)}</span><span class="eng">${icon.heart}${fmtNum(n.likes)}</span>` : `<span>${PLATFORM[n.platform] ?? n.platform}</span>`;
  return `${stamp}<header>${ava}<div class="who"><b>${esc(a.name)}</b><span>${handle}</span></div><span class="catno">No.&thinsp;${no}</span></header>
    ${mark}
    <p class="txt">${esc(n.text)}</p>
    ${n.media ? mediaHtml(n.media) : ""}
    ${flags}
    <footer>${eng}<time>${fmtDate(n.t)}</time></footer>`;
}
