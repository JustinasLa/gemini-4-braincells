// Data model for a trackdown: one submitted post traced back to its earliest known source,
// plus the spread that followed. Verdict values match clusters.status in the API.

export type Status = "coordinated" | "likely_organic" | "insufficient";

import type { LookupResult } from "./data/lookup";

export type Platform = "x" | "bluesky" | "web" | "news" | "forum" | "video" | "chat" | "factcheck" | "print" | "letter" | "text";

/** What a node is in the story. */
export type Role =
  | "origin" // earliest known source
  | "mutation" // a copy that changed the claim/context
  | "relay" // a plain reshare on the path
  | "amplifier" // member of a coordinated cluster (behaviour, never a judgement of the person)
  | "organic" // independent share, normal account behaviour
  | "submitted" // what the user dropped in
  | "source"; // a web page / fact-check found by search

/** How a child relates to its parent. */
export type Relation = "repost" | "quote" | "copy" | "media" | "link" | "reply" | "cites";

/** Which part of the pipeline produced a link or a receipt. */
export type Tool = "detector" | "nemotron" | "oxylabs" | "gemini" | "you" | "archive" | "bluesky";

export interface Author {
  name: string;
  handle: string;
  hue: number; // avatar colour
  ageDays?: number; // account age when the post was made
  followers?: number;
  templated?: boolean; // handle follows a template (e.g. glow_fan_0412)
}

export interface Media {
  kind: "screenshot" | "photo" | "video" | "link" | "plate";
  src?: string; // image for plates (public-domain artwork)
  title: string; // headline in the screenshot / link card title / caption
  domain?: string;
  hue?: number;
  note?: string; // e.g. what Nemotron saw: "masthead cropped out"
}

export interface TraceNode {
  id: string;
  t: number; // epoch ms
  platform: Platform;
  role: Role;
  author: Author;
  text: string;
  media?: Media;
  url?: string;
  reposts?: number;
  likes?: number;
  cluster?: string;
  flags?: string[]; // short behaviour chips ("acct 3 d old", "same text")
  undated?: boolean; // `t` only places the card; the real publication date is unknown (web results)
  timeUnknown?: boolean;
  realData?: { label: string; facts: { label: string; value: string }[]; url?: string; receiptId?: number | null }[];
}

export interface TraceEdge {
  from: string; // earlier (parent)
  to: string; // later (child)
  rel: Relation;
  sim?: number; // 0..1 similarity for copy/media links
  tool?: Tool; // who established the link
  note?: string;
}

export interface Cluster {
  id: string;
  label: string;
  status: Status;
  members: string[];
  stat: string; // "31 accounts · same link · 94 s"
}

export interface Receipt {
  tool: Tool;
  text: string;
  url?: string;
  against?: boolean; // counter-evidence
  id?: number;
  endpoint?: string;
  latencyMs?: number | null;
  requestId?: string | null;
}

export interface TraceStep {
  tool: Tool;
  text: string;
  hop?: number;
  found?: string[]; // node ids confirmed in this step
  rejected?: number; // candidates looked at and rejected
  id?: string;
  state?: "pending" | "running" | "done" | "failed" | "skipped";
  detail?: string | null;
}

export interface TraceCase {
  id: string;
  title: string;
  kicker: string; // short tag ("Out-of-context photo")
  status: Status;
  confidence: number;
  synthetic: boolean; // simulated demo data
  historical?: boolean; // real historical record, written by hand (not run through the pipeline)
  scored?: boolean; // false: no confidence number shown
  live?: boolean; // from the API
  submittedId: string;
  originId: string;
  nodes: TraceNode[];
  edges: TraceEdge[];
  clusters: Cluster[];
  receipts: Receipt[];
  wouldChange: string[];
  steps: TraceStep[];
  summary: string;
  tag?: string; // replaces the status tag ("Not enough evidence"), e.g. "Account suspended"
  findingsOnly?: boolean; // no detector verdict: receipts are search findings, not signs either way
  lookup?: LookupResult;
  mock?: boolean;
}
