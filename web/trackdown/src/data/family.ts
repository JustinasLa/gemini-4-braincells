// The picture's family tree (lookup/models.py Family, built by lookup/family.py).
import type { LookupResult } from "./lookup";
import { pageUrl, type ImageMatch } from "./imageTrace";
import type { TraceCase, TraceEdge, TraceNode } from "../types";

export type FamilyBranch = "caption" | "label" | "poster" | "face_swap" | "edit" | "screenshot";
export interface FamilyText { text: string; where: "top" | "bottom" | "on_picture" | "below_frame" | "watermark" }
export interface FamilyMember {
  id: string;
  image_url?: string | null;
  thumb_url?: string | null;
  page_url?: string | null;
  domain?: string | null;
  engine?: string | null;
  first_seen?: string | null;
  time_source?: "crawl_date" | "page" | "snowflake" | "wayback" | null;
  archived_url?: string | null;
  inliers?: number | null;
  coverage?: number | null;
  relation?: string | null;
  text: FamilyText[];
  frame: "none" | "poster" | "panel" | "screenshot";
  aimed_at: string;
  edits: string[];
  kind: "source" | "copy" | FamilyBranch;
  small?: boolean;
  /** this server's copy of the picture as matched (while the lookup lives) */
  picture?: string | null;
  receipt_id?: number | null;
}
export interface FamilyQuote { source: string; text: string; url: string; cites: string[] }
export interface FamilyNode {
  id: string;
  parent: string | null;
  kind: "milestone" | "source" | "branch" | "variant" | "submitted";
  branch: FamilyBranch | null;
  label: string;
  caption: string | null;
  members: string[];
  date: string | null;
  time_source: string | null;
  aimed_at: string | null;
  change: string | null;
  new: boolean;
  quotes: FamilyQuote[];
  receipt_id: number | null;
}
export interface FamilySource { source: string; name: string; url: string; image_url?: string | null; summary: string; added?: string | null; inliers?: number | null; receipt_id?: number | null }
export interface Family {
  template: string | null;
  sources: FamilySource[];
  members: FamilyMember[];
  nodes: FamilyNode[];
  lineage: string[];
  events: FamilyNode[];
  notes: string[];
}

export const BRANCH_COLOR: Record<FamilyBranch, string> = {
  caption: "#7aa2f0",
  label: "#5fc9a8",
  poster: "#c9b48f",
  face_swap: "#e2533c",
  edit: "#d9783d",
  screenshot: "#b9a6e3",
};
export const BRANCH_NAME: Record<FamilyBranch, string> = {
  caption: "Captions",
  label: "Labels on the scene",
  poster: "Poster frames",
  face_swap: "Face swaps",
  edit: "Photo edits",
  screenshot: "Screenshots",
};

/** the family when the server sent a usable one, else null (older servers send none) */
export function familyOf(result: LookupResult | null | undefined): Family | null {
  const family = (result as (LookupResult & { family?: Family | null }) | null | undefined)?.family;
  if (!family || !Array.isArray(family.nodes) || !family.nodes.some((node) => node?.kind === "submitted")) return null;
  return {
    template: family.template ?? null,
    sources: Array.isArray(family.sources) ? family.sources : [],
    members: Array.isArray(family.members) ? family.members : [],
    nodes: family.nodes,
    lineage: Array.isArray(family.lineage) ? family.lineage : [],
    events: Array.isArray(family.events) ? family.events : [],
    notes: Array.isArray(family.notes) ? family.notes : [],
  };
}

export function familyStep(result: LookupResult | null | undefined) {
  return result?.steps.find((step) => step.id === "family");
}

/** "2007-01-02" | "2005-01" | "2005" -> a time for the axis (a partial date sits mid-period) */
export function dateTime(value?: string | null): number | undefined {
  if (!value) return undefined;
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(value);
  if (!m) return undefined;
  const [y, mo, d] = [Number(m[1]), m[2] ? Number(m[2]) : undefined, m[3] ? Number(m[3]) : undefined];
  return Date.UTC(y, mo ? mo - 1 : 6, d ?? (mo ? 15 : 1));
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function dateLabel(value?: string | null): string {
  if (!value) return "date unknown";
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(value);
  if (!m) return value;
  if (m[3]) return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
  if (m[2]) return `${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
  return m[1];
}
/** how a member's date was established, in words */
export function dateBasis(source?: string | null): string {
  switch (source) {
    case "wayback": return "Internet Archive's first capture";
    case "crawl_date": return "first seen by TinEye's crawler";
    case "page": return "date printed on the page";
    case "snowflake": return "time inside the post id";
    case "source": return "as dated by the source";
    default: return "no date found";
  }
}
/** dates for copies are upper bounds: the copy existed by then */
export function memberDate(member?: FamilyMember | null): string {
  if (!member?.first_seen) return "undated";
  return `by ${dateLabel(member.first_seen.slice(0, 10))}`;
}
export const memberLink = (member?: FamilyMember | null) => pageUrl(member?.page_url) ?? pageUrl(member?.image_url);
export const memberDomain = (member?: FamilyMember | null) => member?.domain || (memberLink(member) ? new URL(memberLink(member)!).hostname.replace(/^www\./, "") : "your upload");
export const sourceShort = (source: string) => source === "Know Your Meme" ? "KYM" : source === "Wikipedia" ? "WIKI" : source.toUpperCase();


const BRANCH_TAG: Record<FamilyBranch, string> = {
  caption: "CAPTION", label: "LABELS", poster: "POSTER FRAME", face_swap: "FACE SWAP", edit: "PHOTO EDIT", screenshot: "SCREENSHOT",
};
const SOURCE_SITE: Record<string, string> = { "Know Your Meme": "knowyourmeme.com", Wikipedia: "wikipedia.org" };

/** A family member as an image match, so the canvas draws it with the meme card. Our own copy of the
 *  picture comes first: many hosts refuse to be shown on another site. */
function asMatch(member: FamilyMember, changes: string[]): ImageMatch & { id: string } {
  return {
    id: member.id, image_url: member.picture || member.image_url, thumb_url: member.picture ? member.image_url : member.thumb_url,
    page_url: member.page_url, domain: member.domain, engine: member.engine, first_seen: member.first_seen, time_source: member.time_source === "wayback" ? "page" : member.time_source,
    relation: member.kind === "source" || member.kind === "copy" ? "same" : "edited", changes, receipt_id: member.receipt_id,
  };
}

/** The family tree drawn on the trace canvas: documented milestones -> source photo -> one card per
 *  branch -> its variants -> the submission. Every link is a lineage edge, so the canvas lays the
 *  family out as a tree on its time axis. The plain image-trace cards of family members give way
 *  to it; image matches outside the family keep their variation links. */
export function appendFamily(trace: TraceCase): TraceCase {
  const family = familyOf(trace.lookup);
  if (!family || !trace.submittedId) return trace;
  const members = new Map(family.members.map((member) => [member.id, member]));
  const familyIds = new Set(family.members.map((member) => member.id));
  const nodes: TraceNode[] = trace.nodes.filter((node) => !familyIds.has(node.id) || node.id === trace.submittedId);
  const edges: TraceEdge[] = trace.edges.filter((edge) => !(edge.rel === "variant" && (familyIds.has(edge.from) || familyIds.has(edge.to))));
  const submitted = nodes.find((node) => node.id === trace.submittedId);
  const anchor = submitted?.t ?? Date.now();
  const idOf = new Map<string, string>([["submitted", trace.submittedId]]);
  const byId = new Map(family.nodes.map((node) => [node.id, node]));
  const childTime = (id: string): number | undefined => {
    const times = family.nodes.filter((n) => n.parent === id).map((n) => dateTime(n.date) ?? childTime(n.id)).filter((t): t is number => t != null);
    return times.length ? Math.min(...times) : undefined;
  };
  for (const node of family.nodes) {
    if (node.kind === "submitted") continue;
    const first = members.get(node.members[0]);
    const t = dateTime(node.date) ?? (node.kind === "branch" ? childTime(node.id) : undefined);
    const base = { t: t ?? anchor - 1, undated: t == null || node.time_source === "crawl_date" || node.time_source === "wayback", timeUnknown: t == null };
    if (node.kind === "milestone") {
      const sources = [...new Set(node.quotes.map((q) => q.source))];
      const id = `family:${node.id}`;
      idOf.set(node.id, id);
      nodes.push({
        ...base, undated: false, timeUnknown: false, id, platform: "web", role: "source",
        author: { name: sources.join(" + "), handle: SOURCE_SITE[sources[0]] ?? sources[0], hue: 40 },
        text: node.label, url: node.quotes[0]?.url,
        flags: [`${dateLabel(node.date)} · as documented`, ...(sources.length > 1 ? ["sources agree"] : [])],
        realData: node.quotes.map((q) => ({ label: q.source, url: q.url, receiptId: node.receipt_id, facts: [{ label: "Quote", value: q.text }, ...q.cites.slice(0, 3).map((c) => ({ label: "Cites", value: c }))] })),
      });
    } else if (node.kind === "branch") {
      const id = `family:${node.id}`;
      idOf.set(node.id, id);
      const kids = family.nodes.filter((n) => n.parent === node.id && n.kind === "variant").length;
      nodes.push({
        ...base, id, platform: "text", role: "mutation",
        author: { name: node.label, handle: BRANCH_TAG[node.branch ?? "caption"], hue: 20 },
        text: `Branch: ${node.change ?? "changed"}. ${kids} variant${kids === 1 ? "" : "s"} found.`,
        flags: [`${BRANCH_TAG[node.branch ?? "caption"]} branch`],
      });
    } else if (first) {
      // the source photo and each variant: the card of its earliest copy
      const id = first.id;
      idOf.set(node.id, id);
      const copies = node.members.length - 1;
      const changes = node.kind === "variant" ? (first.edits.length ? first.edits : [node.change ?? ""]).filter(Boolean) : [];
      nodes.push({
        ...base, id, platform: "web", role: node.kind === "source" ? "origin" : "mutation",
        author: { name: memberDomain(first), handle: memberDomain(first), hue: 100 },
        text: node.kind === "source" ? "Source photo: the earliest copy found with nothing added" : node.label,
        url: memberLink(first) ?? undefined, imageMatch: asMatch(first, changes),
        eyebrow: node.kind === "source" ? `SOURCE PHOTO${family.template ? ` · ${family.template.toUpperCase()}` : ""}` : `${BRANCH_TAG[node.branch ?? "caption"]} · ${node.branch === "face_swap" || node.branch === "edit" ? "VARIANT" : `“${node.label.slice(0, 40)}”`}`,
        flags: [memberDate(first), ...(node.aimed_at ? [`mocks ${node.aimed_at}`] : []), ...(copies > 0 ? [`+${copies} cop${copies === 1 ? "y" : "ies"}`] : []), ...(first.inliers ? [`photo match ${first.inliers} pts`] : [])],
        realData: [{ label: "Family tree member", url: memberLink(first), receiptId: first.receipt_id, facts: [{ label: "Date", value: `${memberDate(first)} (${dateBasis(first.time_source)})` }, { label: "Same photograph", value: first.inliers ? `${first.inliers} matching points` : "the submission" }, ...(first.archived_url ? [{ label: "Archived copy", value: first.archived_url }] : [])] }],
      });
    }
  }
  for (const node of family.nodes) {
    if (!node.parent) continue;
    const from = idOf.get(node.parent), to = idOf.get(node.id);
    if (!from || !to || from === to) continue;
    const parent = byId.get(node.parent);
    const label = node.kind === "milestone" ? undefined
      : node.kind === "branch" ? BRANCH_TAG[node.branch ?? "caption"]
      : node.kind === "submitted" ? (node.new ? (node.branch === "face_swap" ? "NEW FACE SWAP" : node.branch === "edit" ? "NEW EDIT" : "NEW CAPTION") : parent?.kind === "variant" ? "SAME TEXT" : parent?.kind === "source" ? "SAME PHOTO" : undefined)
      : node.kind === "source" ? "FIRST COPY FOUND" : undefined;
    edges.push({ from, to, rel: node.kind === "milestone" || node.kind === "source" ? "history" : "derived", tool: node.kind === "milestone" ? "archive" : "nemotron", label, note: node.change ?? undefined });
  }
  if (submitted) {
    const sub = byId.get("submitted");
    if (sub?.aimed_at && !submitted.flags?.some((f) => f.startsWith("mocks"))) submitted.flags = [...(submitted.flags ?? []), `mocks ${sub.aimed_at}`];
  }
  return { ...trace, nodes, edges };
}
