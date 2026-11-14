// Live data: the team API (api/schemas.py). Everything here is optional; when the API is not
// reachable the app runs on clearly-labelled simulated cases.
import type { Receipt, Status, TraceCase, TraceEdge, TraceNode, Tool } from "../types";
import { hashStr } from "./build";

interface SignalBrief { family?: string; name?: string; score?: number; polarity?: string; summary?: string }
export interface ClusterSummary {
  id: number;
  status: Status;
  score: number;
  confidence?: number | null;
  size?: number | null;
  window_start: string;
  window_end: string;
  synthetic: boolean;
  top_signals: SignalBrief[];
  verdict?: { status: Status; confidence: number; summary?: string | null } | null;
}
interface Member { did: string; role?: string | null; handle?: string | null; account_created_at?: string | null; followers?: number | null; profile_url: string }
interface Post { uri: string; did: string; text?: string | null; created_at?: string | null; seen_at?: string | null; embed_kind?: string | null; embed_urls: string[]; media_hashes: string[]; url?: string | null }
interface Evidence { id: number; kind: "signal" | "media" | "source" | "analyst"; summary?: string | null; detail: Record<string, unknown>; source_url?: string | null; model?: string | null }
interface ClusterDetail extends ClusterSummary {
  signals: { timeline?: { t: number; n: number; kind: string; key: string; uri?: string }[]; member_graph?: { nodes: string[] } };
  members: Member[];
  posts: Post[];
  evidence: Evidence[];
  analyst?: { status: Status; confidence: number; evidence?: { evidence_id: number; why: string }[]; counter_evidence?: string[]; would_change_verdict?: string[] } | null;
}
interface AnalyzeResponse {
  status: "matched" | "insufficient";
  clusters: ClusterSummary[];
  web_findings: { summary: string; detail: Record<string, unknown>; source_url?: string | null; model: string }[];
  notes: string[];
}

async function getJson<T>(url: string, init?: RequestInit, ms = 6000): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { ...init, signal: ctl.signal, headers: { "content-type": "application/json", ...(init?.headers ?? {}) } });
    if (!r.ok) throw new Error(`${r.status}`);
    const ct = r.headers.get("content-type") ?? "";
    if (!ct.includes("json")) throw new Error("not json");
    return (await r.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

const host = (u: string) => {
  try {
    return new URL(u).hostname;
  } catch {
    return u.replace(/^https?:\/\//, "").split("/")[0] || "link";
  }
};

/** API summaries use ASCII comparisons; show them as typeset text. */
const tidy = (s: string) => {
  const t = s.trim().replace(/>=/g, "≥").replace(/<=/g, "≤").replace(/!=/g, "≠");
  return t ? t[0].toUpperCase() + t.slice(1) : t;
};
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const toolFor = (model?: string | null, kind?: string): Tool =>
  /nemotron/i.test(model ?? "") || kind === "media" ? "nemotron" : /oxylabs/i.test(model ?? "") || kind === "source" ? "oxylabs" : /gemini/i.test(model ?? "") || kind === "analyst" ? "gemini" : "detector";

/** Turn a cluster detail into a trace: each shared item's earliest post is the parent of later ones. */
export async function liveCase(id: number): Promise<TraceCase> {
  const d = await getJson<ClusterDetail>(`/api/clusters/${id}`);
  const members = new Map(d.members.map((m) => [m.did, m]));
  const posts = [...d.posts].filter((p) => p.created_at || p.seen_at).sort((a, b) => Date.parse(a.seen_at ?? a.created_at!) - Date.parse(b.seen_at ?? b.created_at!));
  const nodes: TraceNode[] = posts.map((p) => {
    const m = members.get(p.did);
    const created = m?.account_created_at ? Date.parse(m.account_created_at) : undefined;
    const t = Date.parse(p.seen_at ?? p.created_at!);
    const ageDays = created ? Math.max(0, Math.round((t - created) / 86400000)) : undefined;
    const handle = m?.handle ?? p.did.slice(0, 18);
    return {
      id: p.uri,
      t,
      platform: "bluesky",
      role: d.status === "coordinated" ? "amplifier" : d.status === "likely_organic" ? "organic" : "relay",
      author: { name: handle.split(".")[0], handle, hue: hashStr(p.did) % 360, ageDays, followers: m?.followers ?? undefined },
      text: p.text ?? "",
      media: p.embed_urls[0] ? { kind: "link", title: p.embed_urls[0].replace(/^https?:\/\//, "").slice(0, 70), domain: host(p.embed_urls[0]) } : p.media_hashes.length ? { kind: "photo", title: `${p.media_hashes.length} image(s)`, hue: hashStr(p.media_hashes[0]) % 360 } : undefined,
      url: p.url ?? undefined,
      cluster: "live",
      flags: ageDays != null && ageDays < 30 ? [`acct ${ageDays} d old`] : undefined,
    };
  });
  // parent = earliest earlier post sharing a link, image or near-identical text
  const keyOf = (p: Post) => [...p.embed_urls, ...p.media_hashes, (p.text ?? "").toLowerCase().replace(/\W+/g, " ").trim().slice(0, 80)].filter(Boolean);
  const first = new Map<string, string>();
  const edges: TraceEdge[] = [];
  posts.forEach((p) => {
    const keys = keyOf(p);
    const parentKey = keys.find((k) => first.has(k));
    if (parentKey) edges.push({ from: first.get(parentKey)!, to: p.uri, rel: p.embed_urls.includes(parentKey) ? "link" : p.media_hashes.includes(parentKey) ? "media" : "copy", tool: "detector" });
    for (const k of keys) if (!first.has(k)) first.set(k, p.uri);
  });
  if (nodes.length) {
    nodes[0].role = "origin";
    nodes[nodes.length - 1].role = "submitted";
  }
  // Oxylabs sources become source nodes linked to the earliest post
  d.evidence
    .filter((e) => e.kind === "source" && e.source_url)
    .slice(0, 6)
    .forEach((e, i) => {
      const id = `src-${e.id}`;
      nodes.push({ id, t: (nodes[0]?.t ?? Date.now()) + (i + 1) * 60000, undated: true, platform: "factcheck", role: "source", author: { name: host(e.source_url!), handle: host(e.source_url!), hue: 210 }, text: tidy(e.summary ?? ""), url: e.source_url! });
      if (nodes[0]) edges.push({ from: nodes[0].id, to: id, rel: "cites", tool: "oxylabs" });
    });
  const receipts: Receipt[] = d.evidence
    .filter((e) => e.kind !== "analyst" && e.summary)
    .slice(0, 10)
    .map((e) => {
      // `against` means "points away from the verdict", the same as in the case files
      const det = e.detail as { polarity?: string; score?: number };
      const organicSign = det.polarity === "against";
      const coordSign = det.polarity === "for" && Number(det.score ?? 1) > 0;
      const against = d.status === "likely_organic" ? coordSign : organicSign;
      return { tool: toolFor(e.model, e.kind), text: tidy(e.summary!), url: e.source_url ?? undefined, against };
    });
  for (const c of d.analyst?.counter_evidence ?? []) receipts.push({ tool: "gemini", text: tidy(c), against: true });
  const live = nodes.filter((n) => n.cluster === "live").map((n) => n.id);
  return {
    id: `live-${d.id}`,
    title: `Cluster #${d.id}`,
    kicker: "Live cluster",
    status: d.status,
    confidence: d.confidence ?? d.analyst?.confidence ?? d.score,
    synthetic: d.synthetic,
    live: true,
    submittedId: nodes.filter((n) => n.role === "submitted")[0]?.id ?? nodes[0]?.id,
    originId: nodes[0]?.id,
    nodes,
    edges,
    clusters: live.length >= 3 ? [{ id: "live", label: d.status === "coordinated" ? "Coordinated cluster" : "Cluster", status: d.status, members: live, stat: `${d.size ?? d.members.length} accounts` }] : [],
    receipts,
    wouldChange: d.analyst?.would_change_verdict ?? [],
    // no analyst summary: say what the cluster is and point to the evidence, never stitch signals into a "reason"
    summary: d.verdict?.summary
      ? tidy(d.verdict.summary)
      : `The detector grouped ${plural(d.size ?? d.members.length, "account", "accounts")} that acted on the same posts. There is no written summary for this cluster yet; the Evidence tab lists the signals the detector weighed.`,
    steps: [
      { tool: "detector", text: `Loaded ${plural(posts.length, "post", "posts")} from ${plural(d.members.length, "account", "accounts")}`, found: nodes.filter((n) => n.role !== "source").map((n) => n.id) },
      { tool: "oxylabs", text: `${plural(nodes.filter((n) => n.role === "source").length, "source page", "source pages")} checked`, found: nodes.filter((n) => n.role === "source").map((n) => n.id) },
      { tool: "gemini", text: "Verdict from the stored analyst run" },
    ],
  };
}

/** Ask the API about a judge's own input. Returns null if the API is not reachable. */
export async function analyze(input: string): Promise<TraceCase | null> {
  const body = /^https?:\/\//.test(input.trim()) ? { url: input.trim() } : /^@?[\w-]+(\.[\w-]+)+$/.test(input.trim()) ? { handle: input.trim().replace(/^@/, "") } : { text: input };
  let res: AnalyzeResponse;
  try {
    res = await getJson<AnalyzeResponse>("/api/analyze", { method: "POST", body: JSON.stringify(body) }, 45000);
  } catch {
    return null;
  }
  if (res.status === "matched" && res.clusters.length) return liveCase(res.clusters[0].id);
  // not in our window: show the web findings as what they are, no verdict
  const now = Date.now();
  const sub: TraceNode = { id: "you", t: now, platform: "text", role: "submitted", author: { name: "Your input", handle: "pasted text", hue: 0 }, text: input };
  const nodes: TraceNode[] = [sub];
  const edges: TraceEdge[] = []; // search results are related pages, not a known chain: no links drawn
  res.web_findings.forEach((f, i) => {
    const h = f.source_url ? host(f.source_url) : "web";
    const id = `wf-${i}`;
    // the card names the page; what the search found is in the Evidence tab
    nodes.push({ id, t: now - (i + 1) * 3600000, undated: true, platform: "factcheck", role: "source", author: { name: h, handle: h, hue: 210 }, text: "Related page found by web search.", url: f.source_url ?? undefined });
  });
  return {
    id: `adhoc-${now}`,
    title: `“${input.trim().length > 44 ? input.trim().slice(0, 42).trimEnd() + "…" : input.trim()}”`,
    kicker: "Your input",
    status: "insufficient",
    confidence: 0,
    synthetic: false,
    live: true,
    submittedId: "you",
    originId: "you",
    findingsOnly: true,
    nodes,
    edges,
    clusters: [],
    receipts: [
      ...res.notes.map((n) => ({ tool: "detector" as Tool, text: tidy(n) })),
      ...res.web_findings.map((f) => ({ tool: "oxylabs" as Tool, text: tidy(f.summary), url: f.source_url ?? undefined })),
    ],
    wouldChange: ["The post shows up in the accounts and window we monitor"],
    summary: "This post isn't in the accounts and time window we monitor, so there is no coordination verdict. The web results are related pages found by search, not a confirmed chain of copies.",
    steps: [
      { tool: "you", text: "Input submitted", found: ["you"] },
      { tool: "oxylabs", text: plural(res.web_findings.length, "related web result", "related web results"), found: nodes.slice(1).map((n) => n.id) },
      { tool: "detector", text: "Not in our window: abstain" },
    ],
  };
}
