import type { LookupResult } from "./lookup";
import type { TraceCase, TraceEdge, TraceNode, Tool } from "../types";
import { httpUrl } from "../ui/format";

export interface ImageMatch {
  id?: string;
  image_url?: string | null;
  thumb_url?: string | null;
  page_url?: string | null;
  domain?: string | null;
  engine?: string | null;
  first_seen?: string | null;
  time_source?: string | null;
  width?: number | null;
  height?: number | null;
  relation?: string | null;
  distance?: number | null;
  crop_box?: number[] | null;
  edited_region?: number[] | null;
  relation_note?: string | null;
  changes?: string[] | null;
  /** which side `changes` start from: "match" = they turn this copy into the submitted picture,
   *  "upload" = they turn the submitted picture into this copy; null/absent = not stated (older servers).
   *  A reading convention, not a claim about which picture is older or who edited it. */
  changes_from?: "match" | "upload" | string | null;
  /** a published change description taken back because a later date flipped its direction (post
   *  lookups, rare): changes are then empty, which says nothing about the pictures */
  changes_withdrawn?: boolean | null;
  change_kinds?: string[] | null;
  receipt_id?: number | null;
}

export const IMAGE_RELATIONS = ["same", "resized", "cropped", "edited", "unknown"] as const;
export type ImageRelation = typeof IMAGE_RELATIONS[number] | "different";
export const relationOf = (match: ImageMatch): ImageRelation =>
  [...IMAGE_RELATIONS, "different"].includes(match.relation ?? "") ? match.relation as ImageRelation : "unknown";
export const humanLabel = (value: string) => value.replace(/[_-]+/g, " ").replace(/^./, (letter) => letter.toUpperCase());
export const engineLabel = (match: ImageMatch) => match.engine === "tineye" ? "TinEye" : match.engine === "yandex" ? "Yandex" : "Search engine unknown";
/** an http(s) link or undefined: the app-wide URL check */
export const pageUrl = (value?: string | null) => httpUrl(value ?? undefined);
export const imageUrl = (value?: string | null) => typeof value === "string" && (/^data:image\/(png|jpeg|gif|webp|svg\+xml)[;,]/i.test(value) || /^\/(?!\/)/.test(value)) ? value : pageUrl(value);
export const domainOf = (match: ImageMatch) => match.domain || (pageUrl(match.page_url) ? new URL(match.page_url!).hostname : "unknown source");
export const imageAlt = (match: ImageMatch) => match.relation_note || `${relationOf(match)} copy on ${domainOf(match)}`;
export const imageTime = (match: ImageMatch) => {
  if (!["crawl_date", "page", "snowflake"].includes(match.time_source ?? "") || !match.first_seen) return undefined;
  const time = Date.parse(match.first_seen);
  return Number.isFinite(time) ? time : undefined;
};
/** How to read a match's change sentences: the compare heading and the finale's lead-in. */
export function changeDirection(match: ImageMatch): { heading: string; lead: string } {
  if (match.changes_from === "match") return { heading: "This copy → your picture", lead: "From that copy to this picture, as a model reads it:" };
  if (match.changes_from === "upload") return { heading: "Your picture → this copy", lead: "From this picture to that copy, as a model reads it:" };
  return { heading: "", lead: "What changed, as a model reads it:" };
}
export function firstSeenLabel(match: ImageMatch): string {
  const time = imageTime(match);
  if (time == null) return "date unknown";
  const date = new Date(time).toISOString().slice(0, 10);
  if (match.time_source === "crawl_date") return `first seen by TinEye on ${date} · upper bound, not a publish date`;
  if (match.time_source === "page") return `page dated ${date}`;
  return `time from post id · ${new Date(time).toISOString().replace("T", " ").replace(".000Z", " UTC")}`;
}

/** Where a box is drawn: part 1's relate(upload, match) gives 0..1 fractions of the UPLOAD; a box
 *  with a value above 1 is read as pixels of the matched image (the first draft of the contract). */
export type BoxFrame = "upload" | "match";
export function boxFrame(box: unknown): BoxFrame | null {
  if (!Array.isArray(box) || box.length !== 4 || !box.every((value) => typeof value === "number" && Number.isFinite(value))) return null;
  return box.every((value) => value >= 0 && value <= 1) ? "upload" : "match";
}
/** A box as [left, top, width, height] percentages of the frame it belongs to, or null when malformed. */
export function normalizedBox(box: unknown, width?: number | null, height?: number | null): [number, number, number, number] | null {
  const frame = boxFrame(box);
  if (!frame) return null;
  const sized = Number.isFinite(width) && Number.isFinite(height) && width! > 0 && height! > 0;
  if (frame === "match" && !sized) return null;
  const [w, h] = frame === "upload" ? [1, 1] : [width!, height!];
  const [left, top, right, bottom] = (box as number[]).map((value, index) => Math.max(0, Math.min(index % 2 ? h : w, value)));
  if (right <= left || bottom <= top) return null;
  return [left / w * 100, top / h * 100, (right - left) / w * 100, (bottom - top) / h * 100];
}

export function imageTraceState(result: LookupResult) {
  const reverse = result.steps.find((step) => step.id === "reverse_image");
  const variations = result.steps.find((step) => step.id === "variations");
  const seen = new Set<string>();
  const matches = (Array.isArray(result.image_matches) ? result.image_matches : []).filter((match): match is ImageMatch & { id: string } => {
    if (!match || typeof match.id !== "string" || !match.id || match.id === result.subject_id || seen.has(match.id)) return false;
    seen.add(match.id);
    return true;
  });
  // an "original" published (page or post-id time) after the submitted post cannot be its source: it stays a
  // match. A TinEye crawl date is only an upper bound, so a later one says nothing about order.
  const posted = Date.parse(result.posts.find((post) => post.id === result.subject_id)?.created_at ?? "");
  const after = (match: ImageMatch) => match.time_source !== "crawl_date" && (imageTime(match) ?? -Infinity) > posted;
  const origin = matches.find((match) => match.id === result.image_origin_id && relationOf(match) !== "different" && !after(match));
  const groups = IMAGE_RELATIONS.map((relation) => ({ relation, matches: matches.filter((match) => match !== origin && relationOf(match) === relation) }));
  const busy = (step: typeof reverse) => step?.state === "pending" || step?.state === "running";
  return {
    // the server always sends image_matches ([] by default) and skips both steps for inputs with no
    // image: show the meme trace only when there is an image search to report on
    enabled: matches.length > 0 || [reverse, variations].some((step) => step && step.state !== "skipped") || (result.kind === "image" && !!reverse),
    skipped: [reverse, variations].filter((step) => step?.state === "skipped").map((step) => `${step!.label}: skipped${step!.detail ? ` · ${step!.detail}` : ""}`),
    matches, origin, groups,
    different: matches.filter((match) => relationOf(match) === "different"),
    searching: busy(reverse), comparing: busy(variations),
    failures: [reverse, variations].filter((step) => step?.state === "failed").map((step) => `${step!.label}: ${step!.detail || "This step failed; other trace results remain available."}`),
    noEarlier: !origin && reverse?.state !== "skipped" && (matches.length > 0 || reverse?.state === "done") && !busy(reverse) && reverse?.state !== "failed",
  };
}

export function appendImageTrace(trace: TraceCase): TraceCase {
  if (!trace.lookup) return trace;
  const state = imageTraceState(trace.lookup);
  if (!state.enabled) return trace;
  const submitted = trace.nodes.find((node) => node.id === trace.submittedId);
  const anchor = submitted?.t ?? Date.parse(trace.lookup.started_at);
  const existing = new Set(trace.nodes.map((node) => node.id));
  const nodes: TraceNode[] = state.matches.filter((match) => relationOf(match) !== "different" && !existing.has(match.id)).map((match) => ({
    id: match.id, t: imageTime(match) ?? (Number.isFinite(anchor) ? anchor : 0),
    // a TinEye crawl date places the card but is no publication time: undated keeps it off "Posted", gaps and the ruler
    timeUnknown: imageTime(match) == null, undated: imageTime(match) == null || match.time_source === "crawl_date",
    platform: "web", role: match === state.origin ? "origin" : relationOf(match) === "edited" ? "mutation" : "source",
    author: { name: domainOf(match), handle: domainOf(match), hue: 100 },
    text: match.relation_note || `${humanLabel(relationOf(match))} image copy`,
    url: pageUrl(match.page_url), imageMatch: match,
    media: { kind: "photo", src: imageUrl(match.thumb_url) ?? imageUrl(match.image_url), title: imageAlt(match) },
    flags: [firstSeenLabel(match)],
    realData: [{ label: "Image search match", url: pageUrl(match.page_url), receiptId: match.receipt_id, facts: [{ label: "Image date", value: firstSeenLabel(match) }, { label: "Relation", value: relationOf(match) }, { label: "Engine", value: engineLabel(match) }] }],
  }));
  const all = [...trace.nodes, ...nodes];
  const byId = new Map(all.map((node) => [node.id, node]));
  const origin = state.origin ? byId.get(state.origin.id) : undefined;
  // the image original is the trace origin only when it and the submission both have real times (page /
  // post id) in that order; a post origin found by the lookup stays the origin. The origin feeds
  // "Earliest → submitted", "copies back", the earliest-copy line and the dock mark, which would otherwise
  // turn a TinEye crawl date (an upper bound) or a lookup start time into an asserted age or order
  const ordered = !!origin && !!submitted && submitted.id !== origin.id && !origin.undated && !origin.timeUnknown && !submitted.undated && !submitted.timeUnknown && origin.t <= submitted.t;
  const originId = trace.originId || (ordered ? origin!.id : "");
  const note = "Image variation; not evidence of who copied whom";
  const engineTool = (tool?: string): Tool | undefined => tool === "tineye" || tool === "yandex" ? "oxylabs" : tool === "nemotron" ? "nemotron" : undefined;
  // variant links are visual relationships, not copy lineage (graph: aside links). The server's own
  // variant edges (image origin, else the earliest same/resized match, else the subject -> each edited
  // or cropped match) win; without any, the same rule is applied here. Unknown matches get no link.
  const served = (trace.lookup.edges ?? []).filter((edge) => edge.rel === "variant" && byId.has(edge.from) && byId.has(edge.to) && edge.from !== edge.to);
  const parent = origin?.id ?? submitted?.id;
  const edges: TraceEdge[] = served.length
    ? served.map((edge) => ({ from: edge.from, to: edge.to, rel: "variant", tool: engineTool(edge.tool), note: edge.note || note }))
    : parent ? nodes.filter((node) => node.id !== parent && ["edited", "cropped"].includes(relationOf(node.imageMatch!))).map((node) => ({ from: parent, to: node.id, rel: "variant", note })) : [];
  // the submission and its Original: a same-image copy link (lineage) when dates establish the order and
  // the Original is the trace origin, else a visual variant link that claims no order
  if (origin && submitted && submitted.id !== origin.id && !edges.some((edge) => edge.to === submitted.id && edge.from === origin.id)) {
    edges.push(originId === origin.id
      ? { from: origin.id, to: submitted.id, rel: "media", note: "Same picture, first seen before the submitted post" }
      : { from: origin.id, to: submitted.id, rel: "variant", note });
  }
  const validIds = new Set(all.map((node) => node.id));
  return {
    ...trace, nodes: [...trace.nodes, ...nodes],
    originId,
    edges: [...trace.edges, ...edges],
    steps: trace.steps.map((step) => ({ ...step, found: [...new Set([...(step.found ?? []), ...(trace.lookup!.steps.find((raw) => raw.id === step.id)?.found ?? []).filter((id) => validIds.has(id))])] })),
  };
}

// ---------------------------------------------------------------- the finding: what the submitted picture is
export type MemeFindingKind = "original" | "copy" | "edited" | "first" | "seen";
export interface MemeFinding {
  kind: MemeFindingKind;
  /** the reveal's wordmark */
  word: string;
  /** one short line under the word */
  tag: string;
  /** the full sentence for the panel */
  sentence: string;
  color: string;
  /** the match the finding rests on (the earliest older copy), when there is one */
  match?: ImageMatch & { id: string };
}
const FINDING_COLOR: Record<MemeFindingKind, string> = { original: "#e4b36a", first: "#e4b36a", copy: "#c9c4bb", edited: "#d9783d", seen: "#9aa6bf" };
const DAY = 86_400_000;
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const shortDate = (match: ImageMatch) => {
  const time = imageTime(match);
  return time == null ? "date unknown" : `${match.time_source === "crawl_date" ? "seen by " : ""}${new Date(time).toISOString().slice(0, 10)}`;
};

/** What the image search says the submitted picture is, from facts the lookup returned. Never a
 *  coordination call. "pending" while the image search or comparison still runs; null when there is
 *  no image search to report on (a text lookup, or a search that failed or was skipped).
 *
 *  A match is OLDER than the submission when an upload (it was online before the upload; an uploaded
 *  file has no time of its own), or when its date is before the post (a crawl date gets the server's
 *  one-day slack, since it is only an upper bound). The earliest older match decides Copy vs Edited. */
export function memeFinding(result: LookupResult): MemeFinding | "pending" | null {
  const state = imageTraceState(result);
  if (!state.enabled) return null;
  if (state.searching || state.comparing) return "pending";
  const reverse = result.steps.find((step) => step.id === "reverse_image");
  if (!reverse || reverse.state === "failed" || reverse.state === "skipped") return null;
  const subject = result.posts.find((post) => post.id === result.subject_id);
  const posted = Date.parse(subject?.created_at ?? "");
  const upload = result.kind === "image" && !Number.isFinite(posted);
  const what = upload ? "upload" : "post";
  const related = state.matches.filter((match) => relationOf(match) !== "different");
  const make = (kind: MemeFindingKind, word: string, tag: string, sentence: string, match?: ImageMatch & { id: string }): MemeFinding => ({ kind, word, tag, sentence, color: FINDING_COLOR[kind], match });
  if (!related.length) return make("first", "First sighting", "No other copies found", "The image search found no other copy of this picture. It may be new, or not indexed yet.");
  if (!upload && !Number.isFinite(posted)) return make("seen", "Seen before", `${plural(related.length, "matching copy", "matching copies")} found`, `The image search found ${plural(related.length, "matching copy", "matching copies")}, but this post has no time to compare their dates with.`);
  const older = related
    .filter((match) => upload || (imageTime(match) != null && imageTime(match)! + (match.time_source === "crawl_date" ? DAY : 0) < posted))
    // earliest first; undated (uploads only) last, a same-picture match before an edited one
    .sort((a, b) => (imageTime(a) ?? Infinity) - (imageTime(b) ?? Infinity) || Number(relationOf(a) === "edited") - Number(relationOf(b) === "edited"));
  const edits = related.filter((match) => relationOf(match) === "edited").length;
  if (!older.length) {
    const undated = related.filter((match) => imageTime(match) == null).length;
    return make("original", "Original", "Earliest copy we found",
      `No copy we found is dated before this post${undated ? `; ${plural(undated, "undated copy", "undated copies")} could still be older` : ""}. ${plural(related.length - undated, "later copy", "later copies")} found. A search sample, not proof of who made it.`);
  }
  const first = older.find((match) => relationOf(match) !== "unknown");
  if (!first) {
    const match = older[0];
    return make("seen", "Seen before", `Similar picture on ${domainOf(match)}`, `A similar picture was on ${domainOf(match)} before this ${what} (${firstSeenLabel(match)}). How the two differ was not checked.`, match);
  }
  const relation = relationOf(first);
  if (relation === "edited") {
    const changes = (first.changes ?? []).filter(Boolean);
    return make("edited", "Edited", `Changed from a copy on ${domainOf(first)}`,
      `An earlier version of this picture was on ${domainOf(first)} (${firstSeenLabel(first)}), and this one differs from it.${changes.length ? ` ${changeDirection(first).lead} ${changes.slice(0, 2).join(" ")}` : ""}`, first);
  }
  const kind = relation === "cropped" ? "A cropped copy of this picture" : relation === "resized" ? "The same picture, at another size," : "The same picture";
  const others = edits ? ` ${plural(edits, "edited version")} of it ${edits === 1 ? "was" : "were"} also found.` : "";
  return make("copy", "Copy", `Already on ${domainOf(first)} · ${shortDate(first)}`, `${kind} was on ${domainOf(first)} before this ${what} (${firstSeenLabel(first)}).${others}`, first);
}
