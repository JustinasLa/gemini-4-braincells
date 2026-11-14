import { hashStr } from "../data/build";
import type { Media, TraceNode } from "../types";
import { esc, fmtDate, fmtNum, roleOf } from "./format";
import { cropHtml } from "./crop";
import { icon } from "./icons";
import { imageCardHtml } from "./variations";

export const PLATFORM: Record<string, string> = { text: "Submitted text", x: "𝕏", bluesky: "Bluesky", web: "Web page", news: "News", forum: "Forum", video: "Video", chat: "Chat", factcheck: "Web page", print: "Print", letter: "Letter" };

/** Machine-vision boxes over a media plate. Positions are stable per post; the score is the
 *  similarity of the link that brought it in (if any), so the boxes say what the matcher said. */
function cvBoxes(seed: string, score?: number): string {
  let h = hashStr(seed);
  const r = () => ((h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0) % 1000) / 1000;
  const out: string[] = [];
  for (let i = 0; i < 2; i++) {
    const s = 22 + r() * 14;
    const x = i === 0 ? 4 + r() * (44 - s) : 52 + r() * (44 - s);
    const bh = s * 1.25;
    // lower half only, so a box never covers the headline it is reading
    const y = 50 + r() * Math.max(0, 46 - bh);
    // only the link's own similarity is shown; no invented per-box numbers
    const label = score != null && i === 0 ? `MATCH ${score.toFixed(2)}` : i === 0 ? "TEXT" : "OBJ";
    out.push(`<i class="cvbox" style="left:${x.toFixed(1)}%;top:${y.toFixed(1)}%;width:${s.toFixed(1)}%;height:${bh.toFixed(1)}%"><span>${label}</span></i>`);
  }
  return out.join("");
}

function mediaHtml(m: Media, seed: string, score?: number): string {
  const note = m.note ? `<div class="media-note">${esc(m.note)}</div>` : "";
  const cv = m.kind === "link" ? "" : cvBoxes(seed, score);
  switch (m.kind) {
    case "link":
      // a bare URL is implementation detail: show the domain and a plain label instead
      const bare = /^(https?:\/\/|[\w-]+(\.[\w-]+)+\/)\S*$/i.test(m.title);
      return `<div class="media link"><div class="thumb"></div><div class="lk"><div class="dom">${esc(m.domain ?? "link")}</div><div class="lt${bare ? " bare" : ""}">${bare ? "Shared link" : esc(m.title)}</div></div></div>${note}`;
    case "screenshot": {
      const cropped = /crop/i.test(m.note ?? "") || m.domain === "screenshot";
      return `<div class="media shot${cropped ? " cropped" : ""}">${cv}<div class="mast"><span>${esc((m.domain ?? "").toUpperCase())}</span><span class="tag2">SATIRE</span></div><div class="hl">${esc(m.title)}</div><div class="bars"></div></div>${note}`;
    }
    case "photo":
      if (m.src) return `<div class="media source-photo"><img src="${esc(m.src)}" alt="${esc(m.title)}" draggable="false" loading="lazy" referrerpolicy="no-referrer"></div>${note}`;
      return `<div class="media photo">${cv}<span class="cap">${esc(m.title)}</span></div>${note}`;
    case "video":
      return `<div class="media video">${cv}<div class="play"></div><span class="cap">${esc(m.title)}</span></div>${note}`;
    case "plate":
      return `<div class="media plate">${cv}<img src="${m.src}" alt="${esc(m.title)}" draggable="false" loading="lazy"><span class="cap">${esc(m.title)}</span></div>${note}`;
  }
}

const flagClass = (f: string) => (/old|followers|templated|every|first seen|never saw|invented/i.test(f) ? " warn" : /dropped|cropped|changed|satire|altered|lost|differs|no matching post/i.test(f) ? " amber" : "");

/** Dispatch (full card) or slip (compact cluster member). `no` is the catalogue number. */
export function cardHtml(n: TraceNode, compact: boolean, no: number, matchScore?: number): string {
  if (n.imageMatch) return imageCardHtml(n);
  const a = n.author;
  const fresh = (a.ageDays ?? 999) < 30;
  const shield = cropHtml(a.handle, { templated: a.templated, blank: fresh && !a.templated, size: compact ? 22 : 30 });
  // a web result's handle is its domain, already the title: don't say it twice
  const handle = a.handle === a.name ? "" : `${n.platform === "bluesky" || n.platform === "x" ? "@" : ""}${esc(a.handle.replace(/^@/, ""))}`;
  const unknownTime = n.undated || n.timeUnknown;
  if (compact) {
    const t = new Date(n.t);
    const clock = `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}:${String(t.getUTCSeconds()).padStart(2, "0")}`;
    return `<header>${shield}<div class="who"><b>${esc(a.name)}</b>${handle ? `<span>${handle}</span>` : ""}</div></header>
      <div class="chip-meta">${a.ageDays != null ? `<span class="age">${a.ageDays} d old</span>` : ""}${a.followers != null ? `<span>${a.followers} fol.</span>` : ""}<time>${unknownTime ? "time unknown" : clock}</time></div>`;
  }
  const role = roleOf(n.role);
  const mark = role.mark ? `<div class="rolemark">${esc(role.mark)}</div>` : "";
  const stamp = n.role === "submitted" ? `<div class="stamp">SUBMITTED</div>` : "";
  const flags = n.flags?.length ? `<div class="flags">${n.flags.map((f) => `<span class="flag${flagClass(f)}">${esc(f)}</span>`).join("")}</div>` : "";
  const eng = n.reposts != null || n.likes != null ? `<span class="eng">${icon.repost}${fmtNum(n.reposts)}</span><span class="eng">${icon.heart}${fmtNum(n.likes)}</span>` : `<span>${PLATFORM[n.platform] ?? n.platform}</span>`;
  return `${stamp}<header>${shield}<div class="who"><b>${esc(a.name)}</b>${handle ? `<span>${handle}</span>` : ""}</div>${n.platform === "x" ? '<span class="platform-x" aria-label="X post">𝕏</span>' : ""}${unknownTime ? "" : `<span class="catno">#${String(no).padStart(2, "0")}</span>`}</header>
    ${mark}
    <p class="txt">${esc(n.text)}</p>
    ${n.media ? mediaHtml(n.media, n.id, matchScore) : ""}
    ${flags}
    <footer>${eng}<time>${unknownTime ? "time unknown" : fmtDate(n.t)}</time></footer>`;
}
