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
  time_uncertainty_s?: number | null; // the true time lies in [time, time + this]
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
export interface LookupAssessment {
  model: string; summary: string; origin: string | null; corroboration: string | null; spread_pattern: string | null;
  counter_evidence: string[]; would_change: string[]; cites: number[];
  llm_reading: "organic_looking" | "copy_campaign_looking" | "unclear";
  latency_ms: number | null; cost_usd: number | null;
}
export interface LookupResult {
  id: string;
  state: "running" | "done" | "failed";
  error: string | null;
  input: { url: string | null; text: string | null; image?: { sha256: string; mime: string; bytes: number } | null };
  kind: "x_post" | "x_profile" | "bsky_post" | "bsky_profile" | "web" | "text" | "image";
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
  clusters: (ClusterSummary & { match?: string[]; verdict_applies?: boolean })[];
  spread: LookupSpread | null;
  receipts: { id: number; tool: LookupTool; endpoint: string; summary: string; source_url: string | null; query: string | null; request_id: string | null; latency_ms: number | null; cached: boolean; at: string }[];
  /** the primary model reading (Nemotron); a model inference, never the status */
  assessment: LookupAssessment | null;
  /** every model reading in arrival order (primary first); absent on older servers */
  readings?: LookupAssessment[];
  /** true when two readings' llm_reading differ: show both, neither sets the status */
  readings_disagree?: boolean;
  steps: { id: string; tool: string; label: string; state: NonNullable<TraceStep["state"]>; detail: string | null; found: string[]; started_at: string | null; ended_at: string | null }[];
  notes: string[];
}

export class LookupError extends Error {
  constructor(message: string, public unavailable = false) { super(message); }
}

const TIMEOUT_MS = 150000;
/** fallback polling interval when the event stream is unavailable (was 1500 ms) */
export const POLL_MS = 600;
/** the server sends a snapshot as soon as the stream opens; none by then means a broken or buffered stream */
const FIRST_SNAPSHOT_MS = 4000;
/** a stream that stays silent this long is treated as stalled; polling takes over */
const STALL_MS = 25000;

const parseResult = (raw: string): LookupResult => {
  let result: LookupResult;
  try { result = JSON.parse(raw) as LookupResult; }
  catch { throw new LookupError("The lookup service returned an unreadable response. Please try again later."); }
  if (!result || !result.id || !["running", "done", "failed"].includes(result.state) || !Array.isArray(result.posts) || !Array.isArray(result.steps)) {
    throw new LookupError("The lookup service returned an unexpected response. Please try again later.");
  }
  return result;
};

/** Live job updates over Server-Sent Events. Resolves with the final snapshot, or with null when the
 * stream fails or stalls before one arrives (the caller then polls). Rejects only when aborted. */
function streamLookup(id: string, emit: (raw: string, result: LookupResult) => void, signal: AbortSignal, abortError: () => LookupError): Promise<LookupResult | null> {
  if (typeof EventSource === "undefined") return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const source = new EventSource(`/api/lookup/${encodeURIComponent(id)}/events`);
    let finished = false;
    let watchdog = 0;
    const finish = (value: LookupResult | null, error?: LookupError) => {
      if (finished) return;
      finished = true;
      source.close();
      clearTimeout(watchdog);
      signal.removeEventListener("abort", onAbort);
      if (error) reject(error); else resolve(value);
    };
    const arm = (ms: number) => { clearTimeout(watchdog); watchdog = window.setTimeout(() => finish(null), ms); };
    const onAbort = () => finish(null, abortError());
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) return onAbort();
    arm(FIRST_SNAPSHOT_MS);
    source.addEventListener("snapshot", (event) => {
      const raw = (event as MessageEvent<string>).data;
      let result: LookupResult;
      try { result = parseResult(raw); } catch { return finish(null); } // a bad frame: polling re-reads the job
      if (result.id !== id) return finish(null);
      arm(STALL_MS);
      emit(raw, result);
      if (result.state !== "running") finish(result);
    });
    source.addEventListener("ping", () => arm(STALL_MS)); // optional named keep-alive; comment pings are invisible here
    source.onerror = () => finish(null); // includes 404 and dropped connections; never let EventSource auto-reconnect
  });
}

/** what a lookup starts from: a link, post text or an @handle, or an image file (a meme, a screenshot) */
export type LookupRequest = string | Blob;
export const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];
/** the server's cap; larger images are scaled down before upload */
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
const MAX_SIDE = 2048;
/** the uploaded image of an image lookup, served back while its job lives */
export const uploadUrl = (id: string) => `/api/lookup/${encodeURIComponent(id)}/image`;

/** A large photo becomes a JPEG of at most MAX_SIDE px: Nemotron reads it as well, and the upload
 * is faster. GIFs keep their bytes when they fit (the server reads the first frame). */
export async function prepareImage(file: Blob): Promise<Blob> {
  if (!IMAGE_TYPES.includes(file.type)) throw new LookupError("Upload a JPEG, PNG, WebP or GIF image.");
  if (file.size <= 2 * 1024 * 1024 && file.type !== "image/png") return file;
  let bitmap: ImageBitmap;
  try { bitmap = await createImageBitmap(file); }
  catch { if (file.size <= MAX_UPLOAD_BYTES) return file; throw new LookupError("That image could not be read."); }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= 2 * 1024 * 1024) { bitmap.close(); return file; }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#fff"; // JPEG has no alpha: transparent pixels would turn black and hide dark text
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const out = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
  if (out && out.size < file.size) return out;
  if (file.size <= MAX_UPLOAD_BYTES) return file;
  throw new LookupError("That image is larger than 8 MB.");
}

export async function startLookup(input: LookupRequest, onUpdate: (result: LookupResult) => void = () => {}, signal?: AbortSignal): Promise<LookupResult> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, TIMEOUT_MS);
  const abortError = () => new LookupError(signal?.aborted ? "Lookup cancelled." : "Lookup timed out after 150 seconds. Please try again later.");
  let last = "";
  // identical snapshots (polls between changes) are not re-rendered
  const emit = (raw: string, result: LookupResult) => {
    if (raw === last) return;
    last = raw;
    onUpdate(result);
  };
  const request = async (path: string, body?: { url: string } | { text: string } | Blob): Promise<{ raw: string; result: LookupResult }> => {
    let response: Response;
    const image = body instanceof Blob;
    try {
      response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: { "Content-Type": image ? body.type || "application/octet-stream" : "application/json" },
        body: image ? body : body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch {
      if (controller.signal.aborted) throw abortError();
      throw new LookupError("The live lookup service could not be reached.", true);
    }
    if (response.status === 413) throw new LookupError("That image is too large (8 MB at most).");
    if (response.status === 415) throw new LookupError("That file is not an image we can read. Upload a JPEG, PNG, WebP or GIF.");
    if (response.status === 503 && image) throw new LookupError("Image lookups are not configured or are temporarily unavailable.", true);
    if (response.status === 400) throw new LookupError("That input could not be read. Enter an http(s) link, an @handle or at least 5 words of post text.");
    if (response.status === 429) throw new LookupError("Lookup rate limit reached. Please try again later.");
    if (response.status === 503) throw new LookupError("Live lookup is not configured or is temporarily unavailable.", true);
    if (response.status === 404) throw new LookupError("The lookup endpoint or job is unavailable (it may have expired).", true);
    if (!response.ok) throw new LookupError(`Lookup failed (HTTP ${response.status}). Please try again later.`);
    if (!response.headers.get("content-type")?.includes("json")) throw new LookupError("The lookup endpoint is unavailable on this server.", true);
    let raw: string;
    try { raw = await response.text(); }
    catch {
      if (controller.signal.aborted) throw abortError();
      throw new LookupError("The lookup service returned an unreadable response. Please try again later.");
    }
    return { raw, result: parseResult(raw) };
  };
  try {
    let first: { raw: string; result: LookupResult };
    if (typeof input === "string") {
      const text = input.trim();
      if (!text) throw new LookupError("Enter a link, an @handle or some post text first.");
      first = await request("/api/lookup", /^https?:\/\//i.test(text) ? { url: text } : { text });
    } else {
      first = await request("/api/lookup/image", await prepareImage(input));
    }
    emit(first.raw, first.result);
    let result = first.result;
    if (result.state !== "running") return result;
    const streamed = await streamLookup(result.id, (raw, next) => { result = next; emit(raw, next); }, controller.signal, abortError);
    if (streamed) return streamed;
    // fallback: poll the job (the stream failed, stalled or is not served here)
    while (result.state === "running") {
      if (controller.signal.aborted) throw abortError();
      const polled = await request(`/api/lookup/${encodeURIComponent(result.id)}`);
      result = polled.result;
      emit(polled.raw, result);
      if (result.state !== "running") break;
      await new Promise<void>((resolve) => {
        const finish = () => { clearTimeout(poll); controller.signal.removeEventListener("abort", finish); resolve(); };
        const poll = setTimeout(finish, POLL_MS);
        controller.signal.addEventListener("abort", finish, { once: true });
        if (controller.signal.aborted) finish();
      });
    }
    return result;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

/** the primary reading is in: the trace may look finished while a second reading still runs */
export const readingReady = (result: LookupResult) => result.state !== "running"
  || result.steps.some((step) => step.id === "assess" && ["done", "failed", "skipped"].includes(step.state));
/** every reading received so far, primary first (older servers send only `assessment`) */
export const readingsOf = (result: LookupResult): LookupAssessment[] => result.readings?.length ? result.readings : result.assessment ? [result.assessment] : [];
/** the readings differ (the server says so; older servers: compare their llm_reading) */
export const readingsDisagree = (result: LookupResult) => result.readings_disagree ?? new Set(readingsOf(result).map((r) => r.llm_reading)).size > 1;
/** which pipeline tool wrote a reading, from its model name */
export const modelTool = (model: string | null | undefined): Tool => /nemotron/i.test(model ?? "") ? "nemotron" : /gemini/i.test(model ?? "") ? "gemini" : "archive";

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
    const upload = result.kind === "image" && post.role === "submitted";
    const mediaSource = upload ? uploadUrl(result.id) : httpUrl(post.media_urls[0]);
    if (upload) return {
      id: post.id, t: fallbackTime, timeUnknown: true, platform: "web", role: "submitted",
      author: { name: "Your upload", handle: "uploaded image", hue: hashStr(post.id) % 360 },
      text: post.text, url: undefined,
      media: { kind: "photo", src: mediaSource, title: media[0]?.description || "Your uploaded image", note: media[0]?.description ?? undefined },
      flags: ["uploaded image · no time of its own", ...(media[0] && !post.text ? ["no text read in the image"] : [])],
      realData: realData.slice(1),
    };
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
    const start = timeOf(page.time ?? null) ?? (page.published_at?.includes("T") ? timeOf(page.published_at) : undefined);
    const unc = page.time_uncertainty_s ?? (start != null && page.time_source === "search_hint" ? 86400 : 0);
    // place an uncertain time in the middle of its range, and say how uncertain it is
    const time = start == null ? undefined : start + (unc * 1000) / 2;
    // "date only" just for a real calendar date read on the page; other windows as midpoint ± half
    const dateOnly = start != null && page.time_source === "page" && !!page.published_at && page.published_at.trim().length <= 10;
    const half = unc / 2;
    const hint = page.time_source === "search_hint" ? " (search hint)" : "";
    const precision = start == null ? [] : dateOnly ? [`date only: ${page.published_at!.trim()}, time unknown`] : half >= 2 * 86400 ? [`time ±${Math.round(half / 86400)} days${hint}`] : half > 0 ? [`time ±${Math.max(1, Math.round(half / 3600))} h${hint}`] : [];
    const url = httpUrl(page.final_url) ?? httpUrl(page.url);
    nodes.push({
      id: page.id ?? page.url, t: time ?? fallbackTime, timeUnknown: time == null, platform: page.publisher ? "news" : "web", role: "source",
      author: { name: page.publisher || hostOf(page.url), handle: page.author || hostOf(page.url), hue: hashStr(page.url) % 360 },
      text: [page.headline, page.snippet].filter(Boolean).join("\n"), url,
      flags: time == null ? ["time unknown", ...(page.published_hint ? [`date hint: ${page.published_hint}`] : [])] : [...precision, ...(page.time_source === "search_hint" ? ["date from a search result, page not read"] : [])],
      realData: [{ label: "Page facts", url, receiptId: page.receipt_id, facts: facts({ Publisher: page.publisher, Author: page.author, "Published at (as returned)": page.published_at, "Parsed time (UTC)": page.time, "Time source": page.time_source, "Time uncertainty": unc ? `${Math.round(unc / 3600)} h window` : null, "Date hint (not an exact time)": page.published_hint, Headline: page.headline, Claims: page.claims.join("\n"), "Page role": page.role }) }],
    });
  }
  const nodeIds = new Set(nodes.map((node) => node.id));
  const aliases = new Map(result.pages.flatMap((page) => [[page.url, page.id ?? page.url], [`page:${page.url}`, page.id ?? page.url], ...(page.final_url ? [[page.final_url, page.id ?? page.url]] : [])] as [string, string][]));
  const resolve = (id: string) => aliases.get(id) ?? id;
  const edges = result.edges.map((edge) => ({ ...edge, from: resolve(edge.from), to: resolve(edge.to), sim: edge.sim ?? undefined, tool: toolFor(edge.tool), note: edge.note ?? undefined }))
    .filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to) && edge.from !== edge.to);
  // Only a cluster that shared this link or this text carries a verdict for it; a cluster that
  // merely contains an account seen here is context about that account.
  const matched = result.status === "matched" ? result.clusters.find((cluster) => cluster.verdict_applies) : undefined;
  const clusters: Cluster[] = result.clusters.map((cluster) => ({ id: `monitor-${cluster.id}`, label: cluster.verdict_applies ? `Monitored cluster ${cluster.id} · same ${(cluster.match ?? []).filter((m) => m !== "account").join(" + ") || "link"}` : `Monitored cluster ${cluster.id} · shares an account only, not a verdict on this link`, status: cluster.verdict_applies ? cluster.status : "insufficient", members: [], stat: `${cluster.size ?? "Unknown number of"} accounts · detector status ${cluster.status} · membership not supplied by lookup` }));
  if (result.spread && result.spread.n_accounts >= 3) {
    const members = result.posts.filter((post) => ["submitted", "origin", "copy", "quote", "bluesky"].includes(post.role) && (post.similarity ?? 0) >= 0.8).map((post) => post.id);
    clusters.push({ id: "lookup-copies", label: "Observed copy sample · not a verdict", status: "insufficient", members, stat: `${result.spread.n_accounts} accounts · same text · ${result.spread.span_s == null ? "time span unknown" : fmtDur(result.spread.span_s * 1000)}` });
    nodes.filter((node) => members.includes(node.id)).forEach((node) => { node.cluster = "lookup-copies"; });
  }
  const subject = result.subject_id ? resolve(result.subject_id) : "";
  const submittedId = nodeIds.has(subject) ? subject : nodes.find((node) => node.url === result.input.url || node.id === result.input.url)?.id ?? "";
  return {
    id: result.id, title: (result.posts.find((post) => post.id === result.subject_id)?.text?.replace(/\s+/g, " ") || result.input.url || result.input.text || (result.kind === "image" ? "Uploaded image" : "Live lookup")).slice(0, 90),
    kicker: result.kind === "image" ? "Live image lookup" : "Live link lookup", status: matched?.status ?? "insufficient", confidence: matched?.confidence ?? 0, scored: matched?.confidence != null,
    synthetic: false, live: true, submittedId, originId: result.origin_id && nodeIds.has(resolve(result.origin_id)) ? resolve(result.origin_id) : "",
    nodes, edges, clusters,
    receipts: [
      ...result.receipts.map((receipt) => ({ id: receipt.id, tool: toolFor(receipt.tool), text: receipt.summary, url: httpUrl(receipt.source_url), endpoint: receipt.endpoint, latencyMs: receipt.latency_ms, requestId: receipt.request_id })),
      ...(result.assessment?.counter_evidence ?? []).map((text) => ({ tool: modelTool(result.assessment?.model), text, against: true })),
    ],
    wouldChange: result.assessment?.would_change ?? [],
    steps: result.steps.map((step) => ({ id: step.id, tool: toolFor(step.tool), text: step.label, state: step.state, detail: step.detail, found: step.found.map(resolve).filter((id) => nodeIds.has(id)) })),
    // two readings that differ are both shown in the panel; neither is put up front as the summary
    summary: readingsDisagree(result) ? `Two model readings of these receipts differ; both are shown below. ${result.posts.length} posts and ${result.pages.length} pages collected.`
      : result.assessment?.summary || `${result.posts.length} posts and ${result.pages.length} pages collected so far. ${matched ? "Coordination status comes only from the matched monitored cluster." : `Can't tell: this ${result.kind === "image" ? "image's" : "link's"} footprint does not establish coordination.`}`,
    lookup: result,
  };
}
