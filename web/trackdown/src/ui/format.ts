import type { Relation, Role, Status, Tool } from "../types";

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const p2 = (n: number) => String(n).padStart(2, "0");

export function fmtDate(t: number, withTime = true): string {
  const d = new Date(t);
  if (d.getUTCFullYear() < 1900) return `${d.getUTCFullYear()}`;
  const s = `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  return withTime ? `${s} · ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}` : s;
}
export function fmtClock(t: number): string {
  const d = new Date(t);
  if (d.getUTCFullYear() < 1900) return `${d.getUTCFullYear()}`;
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
}
export function fmtShort(t: number): string {
  const d = new Date(t);
  if (d.getUTCFullYear() < 1900) return `${d.getUTCFullYear()}`;
  return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
}
export function fmtDur(ms: number): string {
  const a = Math.abs(ms);
  const s = a / 1000;
  // compare the rounded value, so 59.7 s reads "1 min", not "60 s"
  if (Math.round(s) < 60) return `${Math.round(s)} s`;
  const m = s / 60;
  if (Math.round(m) < 60) return `${Math.round(m)} min`;
  const h = m / 60;
  if (h < 48) return `${h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h)} h`;
  const d = h / 24;
  if (d < 60) return `${Math.round(d)} d`;
  const mo = d / 30.44;
  if (mo < 24) return `${Math.round(mo)} mo`;
  const y = d / 365.25;
  return y >= 10 ? `${Math.round(y)} yr` : `${y.toFixed(1).replace(/\.0$/, "")} yr`;
}
export function fmtNum(n?: number): string {
  if (n == null) return "–";
  if (n >= 999500) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n < 1000) return String(n);
  if (n < 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return Math.round(n / 1000) + "k";
}
export const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

/** `text` escaped, with every occurrence of each of `runs` (exact parts of it, such as the
 *  words an uploaded image's caption shares with this post) wrapped in a highlight. */
export function markRuns(text: string, runs?: string[]): string {
  const spans: [number, number][] = [];
  for (const run of runs ?? []) {
    if (!run) continue;
    for (let i = text.indexOf(run); i >= 0; i = text.indexOf(run, i + run.length)) spans.push([i, i + run.length]);
  }
  if (!spans.length) return esc(text);
  spans.sort((a, b) => a[0] - b[0]);
  let out = "", at = 0;
  for (const [start, end] of spans) {
    if (end <= at) continue;
    const from = Math.max(start, at);
    out += esc(text.slice(at, from)) + `<mark class="caption-hit">${esc(text.slice(from, end))}</mark>`;
    at = end;
  }
  return out + esc(text.slice(at));
}

export const httpUrl = (value?: string | null): string | undefined => {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
};

export const STATUS: Record<Status, { word: string; css: string; short: string; tag: string; color: string }> = {
  coordinated: { word: "Shill", css: "var(--shill)", color: "#e2533c", short: "Shill", tag: "Coordinated spread" },
  likely_organic: { word: "Chill", css: "var(--chill)", color: "#5fc9a8", short: "Chill", tag: "Organic spread" },
  insufficient: { word: "Can't tell", css: "var(--unsure)", color: "#9aa6bf", short: "Can't tell", tag: "Not enough evidence" },
};

/** A cluster's short label: its status, except a lookup's "insufficient" cluster (a copy sample, or a
 *  monitored cluster that shares only an account), which is no call rather than a Can't tell verdict. */
export const clusterShort = (status: Status, lookup: boolean) => lookup && status === "insufficient" ? "No call" : STATUS[status].short;

export const ROLE: Record<Role, { label: string; color: string; mark?: string }> = {
  origin: { label: "Earliest known", color: "#e4b36a", mark: "Earliest known" },
  mutation: { label: "Altered copy", color: "#d9783d", mark: "Altered copy" },
  relay: { label: "Reshare", color: "#8f8b84" },
  amplifier: { label: "In coordinated cluster", color: "#e2533c" },
  organic: { label: "Independent", color: "#5fc9a8" },
  submitted: { label: "Submitted", color: "#f2efe9" },
  source: { label: "Found by search", color: "#7aa2f0", mark: "Found by search" },
  screenshotted: { label: "Real post in the screenshot", color: "#b9a6e3", mark: "Real post in the screenshot" },
};
/** Role style that never throws: an unknown role (e.g. from a newer API) renders as a neutral reshare-grey card. */
export const roleOf = (role: string | undefined): (typeof ROLE)[Role] =>
  (role && Object.prototype.hasOwnProperty.call(ROLE, role) ? ROLE[role as Role] : undefined) ?? { label: "Post", color: "#8f8b84" };

export const REL: Record<Relation, { label: string; dash: number[] }> = {
  variant: { label: "image variation", dash: [3, 5] },
  repost: { label: "reposted", dash: [] },
  quote: { label: "quoted", dash: [] },
  copy: { label: "copied", dash: [6, 4] },
  media: { label: "same image", dash: [1.5, 4] },
  link: { label: "shared link", dash: [] },
  reply: { label: "replied", dash: [] },
  cites: { label: "cites", dash: [10, 5] },
  screenshot_of: { label: "screenshot of", dash: [2, 5] },
  history: { label: "documented history", dash: [] },
  derived: { label: "same photograph", dash: [] },
};
/** Relation style that never throws: an unknown rel renders as a plain "related" line. */
export const relOf = (rel: string | undefined): (typeof REL)[Relation] =>
  (rel && Object.prototype.hasOwnProperty.call(REL, rel) ? REL[rel as Relation] : undefined) ?? { label: "related", dash: [] };
/** Relations that say "this is a copy of that" (text or image match). A screenshot link is not one. */
export const isMatchRel = (rel: string | undefined) => rel === "copy" || rel === "media";
/** Relations that form the copy/spread tree (parent -> child). A screenshot link, or a rel this
 *  app does not know yet, is drawn but kept out of the chain, the path to the earliest copy and lineage. */
const LINEAGE_RELS = new Set<string>(["repost", "quote", "copy", "media", "link", "reply", "cites", "history", "derived"]);
export const isLineageRel = (rel: string | undefined) => rel != null && LINEAGE_RELS.has(rel);
/** A lineage edge: a lineage rel between two posts that are both in the copy/spread tree. A post
 *  shown in a screenshot ("screenshotted") is never in it, even when another edge (a reply to it,
 *  say) would make it a parent: its relationship is the screenshot link, shown on its own. */
export const isLineageEdge = (
  e: { rel?: string },
  from?: { role?: string } | null,
  to?: { role?: string } | null,
) => isLineageRel(e.rel) && from?.role !== "screenshotted" && to?.role !== "screenshotted";

/** Wording for a screenshot receipt's label. It describes the screenshot, never a person. */
export const shotLabel = (label: string | null | undefined, reason?: string | null): string => {
  switch (label) {
    case "matches": return "Matches the real post";
    case "differs": return "Differs from the real post";
    case "not_found": return "No matching post found: it may be deleted, private or never posted";
    default: return `Not checked${reason ? `: ${reason}` : ""}`;
  }
};

export const TOOL: Record<Tool, { label: string; long: string }> = {
  detector: { label: "detector", long: "Behaviour detector" },
  nemotron: { label: "nemotron", long: "NVIDIA Nemotron 3.5 Super VL" },
  oxylabs: { label: "oxylabs", long: "Oxylabs Web API" },
  gemini: { label: "gemini", long: "Gemini analyst" },
  you: { label: "input", long: "Your submission" },
  archive: { label: "archive", long: "Archive / post-ID timestamp" },
  bluesky: { label: "bluesky", long: "Bluesky public AppView" },
  fetch: { label: "fetch", long: "Direct image download" },
};

/** T3 Code's font stacks: the system UI font, Inter (Google Fonts) where there is none. Canvas text
 *  uses these; keep them identical to --sans and --mono in styles.css. */
export const SANS = '-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif';
export const MONO = 'ui-monospace, "SF Mono", "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace';
