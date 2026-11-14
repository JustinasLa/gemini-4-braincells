import type { Cluster, Relation, Role, Tool, TraceCase, TraceNode, TraceStep } from "../types";
import { fmtDur, fmtNum, httpUrl } from "../ui/format";
import type { ClusterSummary } from "./api";
import { hashStr } from "./build";

type LookupTool = "oxylabs" | "nemotron" | "gemini" | "detector" | "bluesky" | "x-id";
export interface LookupPost {
  id: string;
  platform: "x" | "bluesky" | "web";
  url: string | null;
  author_handle: string | null;
  author_name: string | null;
  text: string;
  created_at: string | null;
  time_source: "snowflake" | "api" | "page" | "search_hint" | null;
  role: "submitted" | "origin" | "copy" | "quote" | "reply" | "mention" | "author_recent" | "bluesky";
  similarity: number | null;
  metrics: Partial<Record<"views" | "replies" | "reposts" | "likes" | "bookmarks" | "quotes", number | null>> | null;
  metrics_as_of: string | null;
  card: { title: string | null; domain: string | null; url: string | null } | null;
  media_urls: string[];
  receipt_id: number | null;
  verified?: boolean;
}
export interface LookupAccount {
  platform: LookupPost["platform"];
  handle: string;
  name: string | null;
  url: string | null;
  avatar_url: string | null;
  bio: string | null;
  location: string | null;
  joined: string | null;
  age_days: number | null;
  followers: number | null;
  following: number | null;
  posts_count: number | null;
  recent_posts_per_day: number | null;
  receipt_id: number | null;
}
export interface LookupPage {
  id?: string;
  url: string;
  final_url: string | null;
  role: "linked" | "coverage" | "reference";
  publisher: string | null;
  author: string | null;
  published_at: string | null;
  published_hint: string | null;
  time?: string | null;
  time_source?: "page" | "search_hint" | null;
  headline: string | null;
  claims: string[];
  snippet: string | null;
  receipt_id: number | null;
}
export interface LookupMedia {
  media_url: string;
  post_id: string | null;
  description: string | null;
  extracted_text: string[];
  reuse_cues: string[];
  manipulation_cues: string[];
  confidence: number | null;
  model: string | null;
  latency_ms: number | null;
  receipt_id: number | null;
}
export interface LookupSpread {
  n_posts: number;
  n_accounts: number;
  n_verbatim: number;
  first_at: string | null;
  last_at: string | null;
  span_s: number | null;
  first_hour_accounts: number | null;
  min_gap_s: number | null;
  note: string;
}
export interface LookupResult {
  id: string;
  state: "running" | "done" | "failed";
  error: string | null;
  input: { url: string | null; text: string | null };
  kind: "x_post" | "x_profile" | "bsky_post" | "bsky_profile" | "web" | "text";
  started_at: string;
  finished_at: string | null;
  status: "matched" | "insufficient";
  subject_id: string | null;
  origin_id: string | null;
  posts: LookupPost[];
  accounts: LookupAccount[];
  pages: LookupPage[];
  media: LookupMedia[];
  edges: { from: string; to: string; rel: Relation; sim: number | null; tool: LookupTool; note: string | null }[];
  clusters: ClusterSummary[];
  spread: LookupSpread | null;
  receipts: { id: number; tool: LookupTool; endpoint: string; summary: string; source_url: string | null; query: string | null; request_id: string | null; latency_ms: number | null; cached: boolean; at: string }[];
  assessment: {
    model: string; summary: string; origin: string | null; corroboration: string | null; spread_pattern: string | null;
    counter_evidence: string[]; would_change: string[]; cites: number[];
    llm_reading: "organic_looking" | "copy_campaign_looking" | "unclear";
    latency_ms: number | null; cost_usd: number | null;
  } | null;
  steps: { id: string; tool: string; label: string; state: NonNullable<TraceStep["state"]>; detail: string | null; found: string[]; started_at: string | null; ended_at: string | null }[];
  notes: string[];
}

export class LookupError extends Error {
  constructor(message: string, public unavailable = false) { super(message); }
}

export async function startLookup(input: string, onUpdate: (result: LookupResult) => void = () => {}, signal?: AbortSignal): Promise<LookupResult> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, 150000);
  const request = async (path: string, body?: { url: string } | { text: string }): Promise<LookupResult> => {
    let response: Response;
    try {
      response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch {
      if (controller.signal.aborted) throw new LookupError(signal?.aborted ? "Lookup cancelled." : "Lookup timed out after 150 seconds. Please try again later.");
      throw new LookupError("The live lookup service could not be reached.", true);
    }
    if (response.status === 400) throw new LookupError("That input could not be read. Enter a valid http(s) link or some post text.");
    if (response.status === 429) throw new LookupError("Lookup rate limit reached. Please try again later.");
    if (response.status === 503) throw new LookupError("Live lookup is not configured or is temporarily unavailable.", true);
    if (response.status === 404) throw new LookupError("The lookup endpoint or job is unavailable (it may have expired).", true);
    if (!response.ok) throw new LookupError(`Lookup failed (HTTP ${response.status}). Please try again later.`);
    if (!response.headers.get("content-type")?.includes("json")) throw new LookupError("The lookup endpoint is unavailable on this server.", true);
    let result: LookupResult;
    try { result = await response.json() as LookupResult; }
    catch { throw new LookupError("The lookup service returned an unreadable response. Please try again later."); }
    if (!result.id || !["running", "done", "failed"].includes(result.state) || !Array.isArray(result.posts) || !Array.isArray(result.steps)) {
      throw new LookupError("The lookup service returned an unexpected response. Please try again later.");
    }
    return result;
  };
  try {
    const text = input.trim();
    if (!text) throw new LookupError("Enter a link or some post text first.");
    let result = await request("/api/lookup", /^https?:\/\//i.test(text) ? { url: text } : { text });
    onUpdate(result);
    while (result.state === "running") {
      await new Promise<void>((resolve) => {
        const finish = () => { clearTimeout(poll); controller.signal.removeEventListener("abort", finish); resolve(); };
        const poll = setTimeout(finish, 1500);
        controller.signal.addEventListener("abort", finish, { once: true });
        if (controller.signal.aborted) finish();
      });
      if (controller.signal.aborted) throw new LookupError(signal?.aborted ? "Lookup cancelled." : "Lookup timed out after 150 seconds. Please try again later.");
      result = await request(`/api/lookup/${encodeURIComponent(result.id)}`);
      onUpdate(result);
    }
    return result;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

const toolFor = (tool: string): Tool => {
  if (["oxylabs", "nemotron", "gemini", "detector", "bluesky", "you"].includes(tool)) return tool as Tool;
  return "archive";
};
const timeOf = (value: string | null): number | undefined => {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : undefined;
};
const facts = (values: Record<string, string | number | null | undefined>) => Object.entries(values)
  .map(([label, value]) => ({ label, value: value == null || value === "" ? "Unknown / not returned" : String(value) }));
const hostOf = (url: string) => { try { return new URL(url).hostname; } catch { return "Web source"; } };

export function lookupCase(result: LookupResult): TraceCase {
  const fallbackTime = timeOf(result.posts.find((post) => post.id === result.subject_id)?.created_at ?? null)
    ?? timeOf(result.started_at) ?? 0;
  const nodes: TraceNode[] = result.posts.map((post) => {
    const handle = post.author_handle || "Unknown author";
    const metrics = post.metrics ?? {};
    const account = result.accounts.find((candidate) => candidate.platform === post.platform && candidate.handle.replace(/^@/, "").toLowerCase() === handle.replace(/^@/, "").toLowerCase());
    const media = result.media.filter((finding) => finding.post_id === post.id);
    const time = timeOf(post.created_at);
    const roles: Record<LookupPost["role"], Role> = { submitted: "submitted", origin: "origin", copy: post.similarity != null && post.similarity < 0.8 ? "mutation" : "relay", quote: "mutation", reply: "organic", mention: "relay", author_recent: "relay", bluesky: "relay" };
    const realData: NonNullable<TraceNode["realData"]> = [{
      label: "Platform metrics", url: httpUrl(post.url), receiptId: post.receipt_id,
      facts: facts({ Views: metrics.views, Replies: metrics.replies, Reposts: metrics.reposts, Likes: metrics.likes, Bookmarks: metrics.bookmarks, Quotes: metrics.quotes, "Metrics as of (UTC)": post.metrics_as_of, "Created at (UTC)": post.created_at, "Time source": post.time_source, "Post role": post.role, "Post page/API read": post.verified == null ? null : post.verified ? "Yes" : "No · search snippet / ID only" }),
    }];
    if (post.card) realData.push({ label: "Linked card", url: httpUrl(post.card.url), receiptId: post.receipt_id, facts: facts({ Headline: post.card.title, Domain: post.card.domain }) });
    if (account) realData.push({ label: "Author facts", url: httpUrl(account.url), receiptId: account.receipt_id, facts: facts({ Joined: account.joined, Followers: account.followers, Following: account.following, Posts: account.posts_count, "Recent posts/day (profile sample)": account.recent_posts_per_day, Bio: account.bio, Location: account.location }) });
    for (const finding of media) realData.push({ label: "Nemotron media reading (model inference)", url: httpUrl(finding.media_url), receiptId: finding.receipt_id, facts: facts({ Description: finding.description, "OCR text": finding.extracted_text.join("\n"), "Reuse cues": finding.reuse_cues.join("\n"), "Manipulation cues": finding.manipulation_cues.join("\n"), Model: finding.model, "Latency (ms)": finding.latency_ms }) });
    const mediaSource = httpUrl(post.media_urls[0]);
    return {
      id: post.id, t: time ?? fallbackTime, timeUnknown: time == null, platform: post.platform, role: roles[post.role],
      author: { name: post.author_name || account?.name || handle, handle, hue: hashStr(`${post.platform}:${handle}`) % 360, followers: account?.followers ?? undefined, ageDays: account?.age_days ?? undefined },
      text: post.text, url: httpUrl(post.url), likes: metrics.likes ?? undefined, reposts: metrics.reposts ?? undefined,
      media: mediaSource ? { kind: "photo", src: mediaSource, title: media[0]?.description || "Post media (source image)", note: media[0]?.description ?? undefined }
        : post.card ? { kind: "link", title: post.card.title || "Linked page", domain: post.card.domain ?? undefined } : undefined,
      flags: [
        ...(time == null ? ["time unknown"] : []),
        ...(post.time_source === "snowflake" ? ["time from post id"] : []),
        ...(post.time_source === "search_hint" ? ["time from search hint"] : []),
        ...(account?.age_days != null && account.age_days < 30 ? [`acct ${account.age_days} d old`] : []),
        ...Object.entries(metrics).filter(([, value]) => value != null).map(([label, value]) => `${fmtNum(value!)} ${label}`),
        ...(post.verified === false ? ["search snippet / ID only"] : []),
        ...(post.role === "author_recent" ? ["author history, not necessarily a copy"] : []),
      ], realData,
    };
  });
  for (const page of result.pages) {
    const time = timeOf(page.time ?? null) ?? (page.published_at?.includes("T") ? timeOf(page.published_at) : undefined);
    const url = httpUrl(page.final_url) ?? httpUrl(page.url);
    nodes.push({
      id: page.id ?? page.url, t: time ?? fallbackTime, timeUnknown: time == null, platform: page.publisher ? "news" : "web", role: "source",
      author: { name: page.publisher || hostOf(page.url), handle: page.author || hostOf(page.url), hue: hashStr(page.url) % 360 },
      text: [page.headline, page.snippet].filter(Boolean).join("\n"), url,
      flags: time == null ? ["time unknown", ...(page.published_hint ? [`date hint: ${page.published_hint}`] : [])] : [],
      realData: [{ label: "Page facts", url, receiptId: page.receipt_id, facts: facts({ Publisher: page.publisher, Author: page.author, "Published at (as returned)": page.published_at, "Parsed time (UTC)": page.time, "Time source": page.time_source, "Date hint (not an exact time)": page.published_hint, Headline: page.headline, Claims: page.claims.join("\n"), "Page role": page.role }) }],
    });
  }
  const nodeIds = new Set(nodes.map((node) => node.id));
  const aliases = new Map(result.pages.flatMap((page) => [[page.url, page.id ?? page.url], [`page:${page.url}`, page.id ?? page.url], ...(page.final_url ? [[page.final_url, page.id ?? page.url]] : [])] as [string, string][]));
  const resolve = (id: string) => aliases.get(id) ?? id;
  const edges = result.edges.map((edge) => ({ ...edge, from: resolve(edge.from), to: resolve(edge.to), sim: edge.sim ?? undefined, tool: toolFor(edge.tool), note: edge.note ?? undefined }))
    .filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to) && edge.from !== edge.to);
  const matched = result.status === "matched" ? result.clusters[0] : undefined;
  const clusters: Cluster[] = result.clusters.map((cluster) => ({ id: `monitor-${cluster.id}`, label: `Monitored cluster ${cluster.id}`, status: cluster.status, members: [], stat: `${cluster.size ?? "Unknown number of"} accounts · detector status · membership not supplied by lookup` }));
  if (result.spread && result.spread.n_accounts >= 3) {
    const members = result.posts.filter((post) => ["submitted", "origin", "copy", "quote", "bluesky"].includes(post.role) && (post.similarity ?? 0) >= 0.8).map((post) => post.id);
    clusters.push({ id: "lookup-copies", label: "Observed copy sample · not a verdict", status: "insufficient", members, stat: `${result.spread.n_accounts} accounts · same text · ${result.spread.span_s == null ? "time span unknown" : fmtDur(result.spread.span_s * 1000)}` });
    nodes.filter((node) => members.includes(node.id)).forEach((node) => { node.cluster = "lookup-copies"; });
  }
  const subject = result.subject_id ? resolve(result.subject_id) : "";
  const submittedId = nodeIds.has(subject) ? subject : nodes.find((node) => node.url === result.input.url || node.id === result.input.url)?.id ?? "";
  return {
    id: result.id, title: (result.posts.find((post) => post.id === result.subject_id)?.text || result.input.url || result.input.text || "Live lookup").slice(0, 90),
    kicker: "Live link lookup", status: matched?.status ?? "insufficient", confidence: matched?.confidence ?? 0, scored: matched?.confidence != null,
    synthetic: false, live: true, submittedId, originId: result.origin_id && nodeIds.has(resolve(result.origin_id)) ? resolve(result.origin_id) : "",
    nodes, edges, clusters,
    receipts: [
      ...result.receipts.map((receipt) => ({ id: receipt.id, tool: toolFor(receipt.tool), text: receipt.summary, url: httpUrl(receipt.source_url), endpoint: receipt.endpoint, latencyMs: receipt.latency_ms, requestId: receipt.request_id })),
      ...(result.assessment?.counter_evidence ?? []).map((text) => ({ tool: "gemini" as const, text, against: true })),
    ],
    wouldChange: result.assessment?.would_change ?? [],
    steps: result.steps.map((step) => ({ id: step.id, tool: toolFor(step.tool), text: step.label, state: step.state, detail: step.detail, found: step.found.map(resolve).filter((id) => nodeIds.has(id)) })),
    summary: result.assessment?.summary || `${result.posts.length} posts and ${result.pages.length} pages collected so far. ${matched ? "Coordination status comes only from the matched monitored cluster." : "Can't tell (yet): this link's footprint does not establish coordination."}`,
    lookup: result,
  };
}
