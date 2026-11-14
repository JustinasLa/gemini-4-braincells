// Meme swarms: is one picture being pushed by a swarm (bot network, astroturfing, brigading)?
// The types mirror swarm/model.py field for field (that file is the contract: change both together).
// Below them, the pure logic the UI draws from (dot layout, chips, labels: tested in
// tests/test_trackdown_swarm.mjs) and the two reads of the API. Nothing here touches the DOM.

export type SwarmPlatform = "bluesky" | "x" | "web";
export type SwarmFamily = "timing" | "accounts" | "copy" | "boost";
/** swarm -> "Shill", organic -> "Chill", unclear -> "Can't tell": about the group of posts, never one account */
export type SwarmVerdict = "swarm" | "organic" | "unclear";
export type SwarmBasis = "live" | "sample";
export type SwarmAction = "post" | "repost" | "quote" | "reply" | "like";

export interface SwarmSignal {
  family: SwarmFamily;
  name: string;
  score: number; // 0..1
  polarity: "for" | "against";
  /** a few plain words with the numbers, shown as is: "31 accounts in 4 min" */
  summary: string;
  numbers: Record<string, number | string | null>;
  examples: string[];
}

export interface SwarmBurst { start: string; end: string; n_posts: number; n_accounts: number }

export interface SwarmTimelinePoint {
  t: number; // epoch seconds
  a: number; // account index within the report; not a name
  k: SwarmAction;
  g: number; // same-words group; 0 = own words
  burst: boolean;
}

export interface SwarmReport {
  verdict: SwarmVerdict;
  why_unclear: string;
  score: number;
  guard: number;
  p_value: number | null;
  families: Partial<Record<SwarmFamily, number>>;
  fired: SwarmFamily[];
  signals: SwarmSignal[]; // strongest first, guards last
  n_posts: number;
  n_boosts: number;
  n_accounts: number;
  n_accounts_known: number;
  first_at: string | null;
  last_at: string | null;
  burst: SwarmBurst | null;
  timeline: SwarmTimelinePoint[]; // <= 300, time order
  basis: SwarmBasis;
  basis_note: string;
  version: string;
}

export interface SwarmPost { url: string; platform: SwarmPlatform; handle: string | null; at: string | null; text: string }

export interface SwarmCard {
  id: string;
  thumb: string;
  verdict: SwarmVerdict;
  score: number;
  n_posts: number;
  n_accounts: number;
  first_at: string | null;
  last_at: string | null;
  families: Partial<Record<SwarmFamily, number>>;
  headline: string;
  kind: string | null;
}

export interface SwarmDetail extends SwarmCard {
  picture: string;
  report: SwarmReport;
  posts: SwarmPost[]; // <= 30, earliest first
  caption: string | null;
}

export interface SwarmWall { items: SwarmCard[]; as_of: string | null; window_h: number | null }

/** a lookup's pointer to the live Bluesky group of the same picture (its page is ?swarm=<id>) */
export interface SwarmLiveRef { id: string; verdict: SwarmVerdict; n_accounts: number }

// ---------------------------------------------------------------- words and colours

export const VERDICT: Record<SwarmVerdict, { word: string; css: string }> = {
  swarm: { word: "Shill", css: "var(--shill)" },
  organic: { word: "Chill", css: "var(--chill)" },
  unclear: { word: "Can't tell", css: "var(--unsure)" },
};
export const FAMILY_NAME: Record<SwarmFamily, string> = { timing: "Timing", accounts: "Accounts", copy: "Copy-paste", boost: "Boosting" };

/** fills for same-words groups, largest group first (the painting's other colours, none of the verdict ones) */
export const GROUP_FILLS = ["#e4b36a", "#7aa2f0", "#b9a6e3", "#f2efe9"];

export interface SwarmChip { family: SwarmFamily; label: string; summary: string; score: number }

/** What the verdict rests on, as at most `max` chips. Shill: the strongest "for" signal of each
 *  family that fired. Chill: the strongest "against" signals, unlabelled. Can't tell: none (why_unclear says it). */
export function swarmChips(report: Pick<SwarmReport, "verdict" | "signals" | "fired">, max = 4): SwarmChip[] {
  if (report.verdict === "unclear") return [];
  const signals = (report.signals ?? []).filter((s) => s && s.summary?.trim() && Number.isFinite(s.score));
  const chip = (s: SwarmSignal): SwarmChip => ({ family: s.family, label: FAMILY_NAME[s.family] ?? s.family, summary: s.summary.trim(), score: s.score });
  const byScore = (a: SwarmSignal, b: SwarmSignal) => b.score - a.score;
  if (report.verdict === "organic") {
    // no family label: a guard's family names what it rules out ("Copy-paste" over "14 of 18 own words")
    const seen = new Set<string>();
    return signals.filter((s) => s.polarity === "against").sort(byScore)
      .filter((s) => !seen.has(s.summary.trim()) && !!seen.add(s.summary.trim())).slice(0, max).map((s) => ({ ...chip(s), label: "" }));
  }
  const fired = new Set(report.fired ?? []);
  const best = new Map<SwarmFamily, SwarmSignal>();
  for (const s of signals) {
    if ((s.polarity ?? "for") !== "for") continue;
    if (fired.size && !fired.has(s.family)) continue;
    const held = best.get(s.family);
    if (!held || s.score > held.score) best.set(s.family, s);
  }
  return [...best.values()].sort(byScore).slice(0, max).map(chip);
}

// ---------------------------------------------------------------- the dot timeline

export interface SwarmDot {
  col: number;
  row: number; // 0 = bottom
  t: number;
  boost: boolean; // repost, like, quote, reply
  burst: boolean;
  /** -1: own words; 0..: rank of its same-words group by size (0 = the biggest copy-paste) */
  group: number;
}
export interface SwarmLayout { dots: SwarmDot[]; cols: number; rows: number; burst: [number, number] | null }

/**
 * One dot per sighting, packed into `cols` columns of at most `rows` dots (a histogram that keeps
 * its dots). x is time on a gap-compressed axis: each gap between two sightings counts
 * log(1 + gap / tau), so a day-long wait does not squeeze everything else into one column and a
 * spread of ordinary people stays scattered, while sightings seconds apart land in the same column.
 * Dots fill columns in time order and spill into the next column when one is full, so a burst
 * becomes a solid block. Nothing moves left of an earlier sighting.
 */
export function layoutTimeline(points: readonly SwarmTimelinePoint[], opts: { cols?: number; rows?: number; tau?: number } = {}): SwarmLayout {
  const cols = Math.max(1, Math.floor(opts.cols ?? 48));
  const tau = opts.tau ?? 60;
  const pts = points.filter((p) => p && Number.isFinite(p.t)).slice().sort((a, b) => a.t - b.t);
  const n = pts.length;
  const rows = Math.max(1, Math.floor(opts.rows ?? 10), Math.ceil(n / cols));
  if (!n) return { dots: [], cols, rows, burst: null };

  const pos = [0];
  for (let i = 1; i < n; i++) pos.push(pos[i - 1] + Math.log1p(Math.max(0, pts[i].t - pts[i - 1].t) / tau));
  const total = pos[n - 1];
  // Everything within half an hour is not stretched across the whole width: the axis is then at least
  // as long as four hour-long gaps, so posts seconds apart (5 accounts in 2 s) stay one block, centred
  // below, instead of looking like people who posted all day. Longer spans reach both ends.
  const short = pts[n - 1].t - pts[0].t < 1800;
  const span = short ? Math.max(total, 4 * Math.log1p(3600 / tau)) : total;
  const compact = total < span;
  const target = pos.map((p) => Math.round((p / span) * (cols - 1)));

  // forward: never left of the previous sighting, at most `rows` per column
  const col = new Array<number>(n);
  let cur = -1, count = 0;
  for (let i = 0; i < n; i++) {
    let c = Math.max(target[i], cur);
    if (c === cur && count >= rows) c = cur + 1;
    if (c !== cur) { cur = c; count = 0; }
    col[i] = c;
    count++;
  }
  // backward: whatever spilled past the right edge is pushed back in (n <= cols * rows, so it fits)
  if (col[n - 1] > cols - 1) {
    cur = cols - 1; count = 0;
    for (let i = n - 1; i >= 0; i--) {
      let c = Math.min(col[i], cur);
      if (c === cur && count >= rows) c = cur - 1;
      if (c !== cur) { cur = c; count = 0; }
      col[i] = c;
      count++;
    }
  }
  if (compact) {
    const shift = Math.floor((cols - 1 - (col[n - 1] - col[0])) / 2) - col[0];
    for (let i = 0; i < n; i++) col[i] += shift;
  }

  // same-words groups ranked by size, biggest first; ties by first appearance
  const sizes = new Map<number, number>();
  for (const p of pts) if (p.g > 0) sizes.set(p.g, (sizes.get(p.g) ?? 0) + 1);
  const rank = new Map([...sizes.entries()].sort((a, b) => b[1] - a[1]).map(([g], i) => [g, i]));

  const dots = pts.map((p, i): SwarmDot => ({ col: col[i], row: 0, t: p.t, boost: p.k !== "post", burst: !!p.burst, group: p.g > 0 ? rank.get(p.g)! : -1 }));
  // inside a column (seconds apart) the order is free: stack copy-paste groups at the bottom, biggest
  // first, then own words, then boosts, so a group reads as one band of one colour across a burst
  const order = (d: SwarmDot) => (d.boost ? 2e6 : 0) + (d.group >= 0 ? d.group : 1e6);
  const byCol = new Map<number, SwarmDot[]>();
  for (const d of dots) byCol.set(d.col, [...(byCol.get(d.col) ?? []), d]);
  for (const stack of byCol.values()) stack.sort((a, b) => order(a) - order(b) || a.t - b.t).forEach((d, row) => (d.row = row));
  const inBurst = dots.filter((d) => d.burst).map((d) => d.col);
  return { dots, cols, rows, burst: inBurst.length ? [Math.min(...inBurst), Math.max(...inBurst)] : null };
}

// ---------------------------------------------------------------- labels

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
/** a duration in words for screen readers: "4 minutes", "2 hours", "3 days" */
export function durationWords(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return plural(Math.max(1, s), "second");
  const m = Math.round(s / 60);
  if (m < 90) return plural(m, "minute");
  const h = Math.round(s / 3600);
  if (h < 48) return plural(h, "hour");
  return plural(Math.round(s / 86400), "day");
}
/** the same, short, for the axis: "4 min", "2 h", "3 d" */
export function durationShort(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = s / 3600;
  if (h < 48) return `${h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h)} h`;
  return `${Math.round(s / 86400)} d`;
}

const epoch = (iso: string | null | undefined) => {
  const t = iso ? Date.parse(iso) / 1000 : NaN;
  return Number.isFinite(t) ? t : null;
};

/** "Shill: 31 accounts in 4 minutes": the plot's accessible name */
export function swarmAria(report: Pick<SwarmReport, "verdict" | "n_accounts" | "burst" | "first_at" | "last_at" | "timeline">): string {
  const word = VERDICT[report.verdict]?.word ?? VERDICT.unclear.word;
  const b = report.burst;
  const bs = epoch(b?.start), be = epoch(b?.end);
  if (b && bs != null && be != null && b.n_accounts > 1) return `${word}: ${plural(b.n_accounts, "account")} in ${durationWords(be - bs)}`;
  const times = (report.timeline ?? []).map((p) => p.t).filter(Number.isFinite);
  const first = epoch(report.first_at) ?? (times.length ? Math.min(...times) : null);
  const last = epoch(report.last_at) ?? (times.length ? Math.max(...times) : null);
  const accounts = plural(report.n_accounts, "account");
  return first != null && last != null && last > first ? `${word}: ${accounts} over ${durationWords(last - first)}` : `${word}: ${accounts}`;
}

const p2 = (n: number) => String(n).padStart(2, "0");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** a moment as UTC clock time, with the day when it is not today (UTC, like ui/format.ts): "14:12",
 *  "13 Nov 22:30". Empty for a missing or unreadable time. */
export function utcStamp(seconds: number, now = Date.now() / 1000): string {
  if (!Number.isFinite(seconds)) return "";
  const d = new Date(seconds * 1000);
  const today = new Date(now * 1000);
  const clock = `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
  const sameDay = d.getUTCFullYear() === today.getUTCFullYear() && d.getUTCMonth() === today.getUTCMonth() && d.getUTCDate() === today.getUTCDate();
  return sameDay ? clock : `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${clock}`;
}

/** a post's time for its row: `utcStamp` of an ISO time, empty when missing */
export const postStamp = (iso: string | null | undefined, now = Date.now() / 1000) => utcStamp(epoch(iso) ?? NaN, now);

/** the two axis labels: the first sighting's UTC clock time (with the day when it is not today) and
 *  how long after it the last one came */
export function axisLabels(first: number, last: number, now = Date.now() / 1000): [string, string] {
  return [utcStamp(first, now), last > first ? `+${durationShort(last - first)}` : ""];
}

/** the first few words of a post, for a one-line link */
export function firstWords(text: string, words = 8): string {
  const parts = (text ?? "").replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
  return parts.slice(0, words).join(" ") + (parts.length > words ? "…" : "");
}

// ---------------------------------------------------------------- reads

/** the development mock (`?mockSwarm`, dev server only) */
const mocked = () => import.meta.env.DEV && new URLSearchParams(location.search).has("mockSwarm");

async function getJson<T>(url: string, ms = 8000): Promise<T | null> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, headers: { accept: "application/json" } });
    if (!r.ok || !(r.headers.get("content-type") ?? "").includes("json")) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** GET /api/swarms: the wall. Any failure is an empty wall (the home then shows nothing). */
export async function fetchSwarmWall(limit = 24): Promise<SwarmCard[]> {
  const wall = mocked() ? (await import("./mockSwarm")).mockWall() : await getJson<SwarmWall>(`/api/swarms?limit=${limit}`);
  return Array.isArray(wall?.items) ? wall.items.filter((c) => c && typeof c.id === "string" && typeof c.thumb === "string").slice(0, limit) : [];
}

/** GET /api/swarms/{id}; null when it is missing or the API is down */
export async function fetchSwarmDetail(id: string): Promise<SwarmDetail | null> {
  const detail = mocked() ? (await import("./mockSwarm")).mockDetail(id) : await getJson<SwarmDetail>(`/api/swarms/${encodeURIComponent(id)}`);
  return detail && typeof detail.picture === "string" && detail.report ? detail : null;
}
