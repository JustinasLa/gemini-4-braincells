import type { Relation, Role, Status, Tool } from "../types";

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const p2 = (n: number) => String(n).padStart(2, "0");

export function fmtDate(t: number, withTime = true): string {
  const d = new Date(t);
  const s = `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  return withTime ? `${s} · ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}` : s;
}
export function fmtClock(t: number): string {
  const d = new Date(t);
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
}
export function fmtShort(t: number): string {
  const d = new Date(t);
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
  return `${y.toFixed(1).replace(/\.0$/, "")} yr`;
}
export function fmtNum(n?: number): string {
  if (n == null) return "–";
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

export const STATUS: Record<Status, { word: string; emoji: string; css: string; short: string; tag: string }> = {
  coordinated: { word: "Shill", emoji: "🚩", css: "var(--shill)", short: "Shill", tag: "Coordinated spread" },
  likely_organic: { word: "Chill", emoji: "😎", css: "var(--chill)", short: "Chill", tag: "Certified organic" },
  insufficient: { word: "Can't tell (yet)", emoji: "🤷", css: "var(--unsure)", short: "Can't tell", tag: "Not enough evidence" },
};

export const ROLE: Record<Role, { label: string; color: string; badge?: string }> = {
  origin: { label: "Earliest known", color: "#ffc14d", badge: "Earliest known source" },
  mutation: { label: "Context changed", color: "#ff8a4c", badge: "Context changed" },
  relay: { label: "Reshare", color: "#8b93a7" },
  amplifier: { label: "In coordinated cluster", color: "#ff4757" },
  organic: { label: "Independent", color: "#2fd6a3" },
  submitted: { label: "You submitted", color: "#ffffff", badge: "You submitted this" },
  source: { label: "Found by search", color: "#4da3ff", badge: "Found by search" },
};

export const REL: Record<Relation, { label: string; dash: number[] }> = {
  repost: { label: "reposted", dash: [] },
  quote: { label: "quoted", dash: [] },
  copy: { label: "copied text", dash: [7, 5] },
  media: { label: "same image", dash: [2, 5] },
  link: { label: "shared link", dash: [] },
  reply: { label: "replied", dash: [] },
  cites: { label: "cites", dash: [10, 6] },
};

export const TOOL: Record<Tool, { label: string; long: string }> = {
  detector: { label: "Detector", long: "Behaviour detector" },
  nemotron: { label: "Nemotron VL", long: "NVIDIA Nemotron 3.5 Super VL" },
  oxylabs: { label: "Oxylabs", long: "Oxylabs Web API" },
  gemini: { label: "Gemini", long: "Gemini analyst" },
  you: { label: "You", long: "Your submission" },
};

export const toolChip = (t: Tool) => `<span class="tool-chip tool-${t}">${TOOL[t].label}</span>`;
