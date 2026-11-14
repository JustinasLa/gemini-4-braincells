import type { LookupResult } from "./lookup";
import type { TraceCase, TraceEdge, TraceNode } from "../types";

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
  change_kinds?: string[] | null;
  receipt_id?: number | null;
}

export const IMAGE_RELATIONS = ["same", "resized", "cropped", "edited", "unknown"] as const;
export type ImageRelation = typeof IMAGE_RELATIONS[number] | "different";
export const relationOf = (match: ImageMatch): ImageRelation =>
  [...IMAGE_RELATIONS, "different"].includes(match.relation ?? "") ? match.relation as ImageRelation : "unknown";
export const humanLabel = (value: string) => value.replace(/[_-]+/g, " ").replace(/^./, (letter) => letter.toUpperCase());
export const engineLabel = (match: ImageMatch) => match.engine === "tineye" ? "TinEye" : match.engine === "yandex" ? "Yandex" : "Search engine unknown";
export const pageUrl = (value?: string | null) => {
  try { const url = new URL(value ?? ""); return ["http:", "https:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
};
export const imageUrl = (value?: string | null) => typeof value === "string" && (/^data:image\/(png|jpeg|gif|webp|svg\+xml)[;,]/i.test(value) || /^\/(?!\/)/.test(value)) ? value : pageUrl(value);
export const domainOf = (match: ImageMatch) => match.domain || (pageUrl(match.page_url) ? new URL(match.page_url!).hostname : "unknown source");
export const imageAlt = (match: ImageMatch) => match.relation_note || `${relationOf(match)} copy on ${domainOf(match)}`;
export const imageTime = (match: ImageMatch) => {
  if (!["crawl_date", "page", "snowflake"].includes(match.time_source ?? "") || !match.first_seen) return undefined;
  const time = Date.parse(match.first_seen);
  return Number.isFinite(time) ? time : undefined;
};
export function firstSeenLabel(match: ImageMatch): string {
  const time = imageTime(match);
  if (time == null) return "date unknown";
  const date = new Date(time).toISOString().slice(0, 10);
  if (match.time_source === "crawl_date") return `first seen by TinEye on ${date} · upper bound, not a publish date`;
  if (match.time_source === "page") return `page dated ${date}`;
  return `time from post id · ${new Date(time).toISOString().replace("T", " ").replace(".000Z", " UTC")}`;
}

export function normalizedBox(box: unknown, width?: number | null, height?: number | null): [number, number, number, number] | null {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width! <= 0 || height! <= 0 || !Array.isArray(box) || box.length !== 4 || !box.every((value) => typeof value === "number" && Number.isFinite(value))) return null;
  const [left, top, right, bottom] = box.map((value, index) => Math.max(0, Math.min(index % 2 ? height! : width!, value)));
  if (right <= left || bottom <= top) return null;
  return [left / width! * 100, top / height! * 100, (right - left) / width! * 100, (bottom - top) / height! * 100];
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
    enabled: result.image_matches != null || !!reverse || !!variations,
    matches, origin, groups,
    different: matches.filter((match) => relationOf(match) === "different"),
    searching: busy(reverse), comparing: busy(variations),
    failures: [reverse, variations].filter((step) => step?.state === "failed").map((step) => `${step!.label}: ${step!.detail || "This step failed; other trace results remain available."}`),
    noEarlier: !origin && (result.image_matches != null || reverse?.state === "done") && !busy(reverse) && reverse?.state !== "failed",
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
  const origin = nodes.find((node) => node.id === state.origin?.id);
  const parent = origin?.id ?? submitted?.id;
  const note = "Image variation; not evidence of who copied whom";
  const edges: TraceEdge[] = parent ? nodes.filter((node) => node.id !== parent).map((node) => ({ from: parent, to: node.id, rel: "variant", note })) : [];
  // the submission is itself a copy of the original image: link it, after any post lineage it already has
  // (the graph walks parents[0] to the origin, so an existing copy chain keeps priority)
  if (origin && submitted && submitted.id !== origin.id) edges.push({ from: origin.id, to: submitted.id, rel: "variant", note });
  const ids = new Set(state.matches.map((match) => match.id));
  const validIds = new Set([...trace.nodes, ...nodes].map((node) => node.id));
  return {
    ...trace, nodes: [...trace.nodes, ...nodes],
    originId: trace.originId || origin?.id || "", // a post origin, when the lookup found one, stays the origin
    edges: [...trace.edges.filter((edge) => edge.rel !== "variant" || (!ids.has(edge.to) && !ids.has(edge.from))), ...edges],
    steps: trace.steps.map((step) => ({ ...step, found: [...new Set([...(step.found ?? []), ...(trace.lookup!.steps.find((raw) => raw.id === step.id)?.found ?? []).filter((id) => validIds.has(id))])] })),
  };
}
