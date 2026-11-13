import { hashStr } from "../data/build";
import type { Media, TraceNode } from "../types";
import { esc, fmtDate, fmtNum, ROLE } from "./format";
import { arms, armsSeed } from "./heraldry";
import { icon } from "./icons";

const PLATFORM: Record<string, string> = { bluesky: "Bluesky", web: "Web page", news: "News", forum: "Forum", video: "Video", chat: "Chat", factcheck: "Web page", print: "Print", letter: "Letter" };

/** Machine-vision boxes over a media plate. Positions are stable per post; the score is the
 *  similarity of the link that brought it in (if any), so the boxes say what the matcher said. */
function cvBoxes(seed: string, score?: number): string {
  let h = hashStr(seed);
  const r = () => ((h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0) % 1000) / 1000;
  const out: string[] = [];
  for (let i = 0; i < 2; i++) {
    const s = 22 + r() * 14;
    const x = i === 0 ? 4 + r() * (44 - s) : 52 + r() * (44 - s);
    const y = 24 + r() * (60 - s);
    // only the link's own similarity is shown; no invented per-box numbers
    const label = score != null && i === 0 ? `MATCH ${score.toFixed(4)}` : i === 0 ? "TEXT" : "OBJ";
    out.push(`<i class="cvbox" style="left:${x.toFixed(1)}%;top:${y.toFixed(1)}%;width:${s.toFixed(1)}%;height:${(s * 1.25).toFixed(1)}%"><span>${label}</span></i>`);
  }
  return out.join("");
}

function mediaHtml(m: Media, seed: string, score?: number): string {
  const note = m.note ? `<div class="media-note">${esc(m.note)}</div>` : "";
  const cv = m.kind === "link" ? "" : cvBoxes(seed, score);
  switch (m.kind) {
    case "link":
      return `<div class="media link"><div class="thumb"></div><div class="lk"><div class="dom">${esc(m.domain ?? "link")}</div><div class="lt">${esc(m.title)}</div></div></div>${note}`;
    case "screenshot": {
      const cropped = /crop/i.test(m.note ?? "") || m.domain === "screenshot";
      return `<div class="media shot${cropped ? " cropped" : ""}">${cv}<div class="mast"><span>${esc((m.domain ?? "").toUpperCase())}</span><span class="tag2">SATIRE</span></div><div class="hl">${esc(m.title)}</div><div class="bars"></div></div>${note}`;
    }
    case "photo":
      return `<div class="media photo">${cv}<span class="cap">${esc(m.title)}</span></div>${note}`;
    case "video":
      return `<div class="media video">${cv}<div class="play"></div><span class="cap">${esc(m.title)}</span></div>${note}`;
    case "plate":
      return `<div class="media plate">${cv}<img src="${m.src}" alt="${esc(m.title)}" draggable="false" loading="lazy"><span class="cap">${esc(m.title)}</span></div>${note}`;
  }
}

const flagClass = (f: string) => (/old|followers|templated|every|first seen|never saw|invented/i.test(f) ? " warn" : /dropped|cropped|changed|satire|altered|lost/i.test(f) ? " amber" : "");

/** Dispatch (full card) or slip (compact cluster member). `no` is the catalogue number. */
export function cardHtml(n: TraceNode, compact: boolean, no: number, matchScore?: number): string {
  const a = n.author;
  const fresh = (a.ageDays ?? 999) < 30;
  const shield = arms(armsSeed(a.handle, a.templated), { blank: fresh && !a.templated, size: compact ? 18 : 28 });
  const handle = `${n.platform === "bluesky" ? "@" : ""}${esc(a.handle)}`;
  if (compact) {
    const t = new Date(n.t);
    const clock = `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}:${String(t.getUTCSeconds()).padStart(2, "0")}`;
    return `<header>${shield}<div class="who"><b>${esc(a.name)}</b><span>${handle}</span></div></header>
      <div class="chip-meta">${a.ageDays != null ? `<span class="age">${a.ageDays} D OLD</span>` : ""}${a.followers != null ? `<span>${a.followers} FOL.</span>` : ""}<time>${clock}</time></div>`;
  }
  const mark = ROLE[n.role].mark ? `<div class="rolemark">${esc(ROLE[n.role].mark!)}</div>` : "";
  const stamp = n.role === "submitted" ? `<div class="stamp">EXHIBIT A</div>` : "";
  const flags = n.flags?.length ? `<div class="flags">${n.flags.map((f) => `<span class="flag${flagClass(f)}">${esc(f)}</span>`).join("")}</div>` : "";
  const eng = n.reposts != null || n.likes != null ? `<span class="eng">${icon.repost}${fmtNum(n.reposts)}</span><span class="eng">${icon.heart}${fmtNum(n.likes)}</span>` : `<span>${PLATFORM[n.platform] ?? n.platform}</span>`;
  return `${stamp}<header>${shield}<div class="who"><b>${esc(a.name)}</b><span>${handle}</span></div><span class="catno">#${String(no).padStart(2, "0")}</span></header>
    ${mark}
    <p class="txt">${esc(n.text)}</p>
    ${n.media ? mediaHtml(n.media, n.id, matchScore) : ""}
    ${flags}
    <footer>${eng}<time>${fmtDate(n.t)}</time></footer>`;
}
