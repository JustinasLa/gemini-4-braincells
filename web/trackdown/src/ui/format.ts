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
  if (s < 60) return `${Math.round(s)} s`;
  const m = s / 60;
  if (m < 60) return `${Math.round(m)} min`;
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
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n < 1000) return String(n);
  if (n < 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return Math.round(n / 1000) + "k";
}
export const initials = (name: string) =>
  name
    .replace(/[^\p{L}\p{N} ]/gu, "")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("") || "?";

export const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

export const httpUrl = (value?: string | null): string | undefined => {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
};

export const STATUS: Record<Status, { word: string; css: string; short: string; tag: string; color: string }> = {
  coordinated: { word: "Shill", css: "var(--shill)", color: "#e2533c", short: "Shill", tag: "Coordinated spread" },
  likely_organic: { word: "Chill", css: "var(--chill)", color: "#5fc9a8", short: "Chill", tag: "Organic spread" },
  insufficient: { word: "Can't tell (yet)", css: "var(--unsure)", color: "#9aa6bf", short: "Can't tell", tag: "Not enough evidence" },
};

export const ROLE: Record<Role, { label: string; color: string; mark?: string }> = {
  origin: { label: "Earliest known", color: "#e4b36a", mark: "Earliest known" },
  mutation: { label: "Altered copy", color: "#d9783d", mark: "Altered copy" },
  relay: { label: "Reshare", color: "#8f8b84" },
  amplifier: { label: "In coordinated cluster", color: "#e2533c" },
  organic: { label: "Independent", color: "#5fc9a8" },
  submitted: { label: "Submitted", color: "#f2efe9" },
  source: { label: "Found by search", color: "#7aa2f0", mark: "Found by search" },
};

export const REL: Record<Relation, { label: string; dash: number[] }> = {
  repost: { label: "reposted", dash: [] },
  quote: { label: "quoted", dash: [] },
  copy: { label: "copied", dash: [6, 4] },
  media: { label: "same image", dash: [1.5, 4] },
  link: { label: "shared link", dash: [] },
  reply: { label: "replied", dash: [] },
  cites: { label: "cites", dash: [10, 5] },
};

export const TOOL: Record<Tool, { label: string; long: string }> = {
  detector: { label: "detector", long: "Behaviour detector" },
  nemotron: { label: "nemotron", long: "NVIDIA Nemotron 3.5 Super VL" },
  oxylabs: { label: "oxylabs", long: "Oxylabs Web API" },
  gemini: { label: "gemini", long: "Gemini analyst" },
  you: { label: "input", long: "Your submission" },
  archive: { label: "archive", long: "Archive / post-ID timestamp" },
  bluesky: { label: "bluesky", long: "Bluesky public AppView" },
};

export const ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"];
export const roman = (n: number) => ROMAN[n - 1] ?? String(n);
