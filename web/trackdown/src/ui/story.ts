// The meme story: the result of a picture trace as a page of pictures with very few words.
// Your meme, a two-word answer, the text that was added, the original photo and the other versions.
// Everything is read from the lookup result (data/lookup.ts, data/family.ts); the map of everything
// collected, with its receipts and caveats, stays one click away.
import { dateLabel, familyOf, memberDomain, type Family, type FamilyMember, type FamilyNode } from "../data/family";
import { domainOf, imageTraceState, imageUrl, memeFinding, pageUrl, relationOf, type ImageMatch } from "../data/imageTrace";
import { hasVerdict, type LookupResult } from "../data/lookup";
import type { TraceCase } from "../types";
import { comparisonSource } from "./compare";
import { esc, STATUS } from "./format";
import "./story.css";

const cap = (s: string) => s.replace(/^./, (c) => c.toUpperCase());
/** a documented milestone in a few words: its date is shown beside it, the full sentence is the tooltip */
function short(sentence: string, words = 9): string {
  const parts = sentence.replace(/^(?:On|In)\b[^,]{0,40},(?:\s*\d{4},)?\s*/i, "").split(/\s+/);
  return cap(parts.slice(0, words).join(" ")) + (parts.length > words ? "…" : "");
}

/** a picture trace gets the story: an upload, or any lookup whose picture got a family tree */
export const hasStory = (trace: TraceCase | null | undefined): trace is TraceCase & { lookup: LookupResult } =>
  !!trace?.lookup && (trace.lookup.kind === "image" || !!familyOf(trace.lookup));

/** a picture frame that falls back to a second address, then to a plain placeholder */
function picture(primary: string | undefined, fallback: string | undefined, alt: string): string {
  // a blob: address is the reader's own file, made by this page (main.ts)
  const ok = (value?: string) => (value?.startsWith("blob:") ? value : imageUrl(value));
  const src = ok(primary) ?? ok(fallback);
  const next = ok(primary) && ok(fallback) && ok(fallback) !== src ? ok(fallback)! : "";
  return src
    ? `<img data-story-img src="${esc(src)}" data-fallback="${esc(next)}" alt="${esc(alt)}" loading="lazy" referrerpolicy="no-referrer" draggable="false">`
    : `<span class="st-noimg">No picture</span>`;
}
const memberPicture = (member: FamilyMember | undefined, alt: string) => picture(member?.picture ?? undefined, member?.image_url ?? member?.thumb_url ?? undefined, alt);

interface Head { title: string; lead: string; tone: "new" | "known" | "plain" | "busy" | "fail" }

function head(lookup: LookupResult, family: Family | null): Head {
  const sub = family?.nodes.find((node) => node.kind === "submitted");
  const finding = memeFinding(lookup);
  const found = finding && finding !== "pending" ? finding : null;
  if (family && sub) {
    // text on the picture is shown as chips; an edit without text needs its one sentence
    if (sub.caption) return sub.new ? { tone: "new", title: "New version", lead: "" } : { tone: "known", title: "Already out there", lead: "" };
    if (found?.kind === "copy") return { tone: "known", title: "Already out there", lead: found.tag };
    // the comparison model writes "in the later image": here there is only the reader's picture
    const change = cap((sub.change ?? "").trim().replace(/\s+in the (?:later|second|newer) (?:image|picture|version)\b/gi, ""));
    return { tone: sub.new ? "new" : "plain", title: sub.new ? "Edited version" : "Known picture", lead: change };
  }
  if (lookup.state === "running") return { tone: "busy", title: "Tracing…", lead: "" };
  if (found) {
    // "seen" is a look-alike the comparison could not confirm: not a copy, so not "seen before"
    if (found.kind === "seen") return { tone: "plain", title: "No copy found", lead: "Only look-alikes" };
    const title = found.kind === "first" ? "Found nowhere else" : found.kind === "original" ? "Earliest copy found" : found.kind === "edited" ? "Edited copy" : "Already out there";
    return { tone: found.kind === "first" || found.kind === "original" ? "new" : "known", title, lead: found.tag };
  }
  if (lookup.state === "failed") return { tone: "fail", title: "Trace failed", lead: lookup.error || "" };
  return { tone: "plain", title: "No result", lead: "" };
}

/** while it runs: how far along it is, nothing else (step names are written for the finished list) */
function progress(lookup: LookupResult): string {
  const done = lookup.steps.filter((candidate) => candidate.state === "done").length;
  const total = lookup.steps.filter((candidate) => candidate.state !== "skipped").length;
  return `<p class="st-step" role="status"><i></i>Step ${Math.min(done + 1, total)} of ${total}</p>`;
}

function card(id: string, image: string, caption: string, domain: string, label: string): string {
  return `<button class="st-card" type="button" data-meme-compare="${esc(id)}" aria-label="${esc(label)}">
    <span class="st-thumb">${image}</span>
    ${caption ? `<span class="st-cap">${caption}</span>` : ""}
    <span class="st-meta">${esc(domain)}</span>
  </button>`;
}
function versionCard(node: FamilyNode, members: Map<string, FamilyMember>): string {
  const member = members.get(node.members[0])!;
  const text = node.branch === "face_swap" || node.branch === "edit" || node.branch === "screenshot" ? esc(cap(node.label)) : `“${esc(node.label)}”`;
  return card(member.id, memberPicture(member, `Version on ${memberDomain(member)}`), text, memberDomain(member), "Compare with your picture");
}
function matchCard(match: ImageMatch & { id: string }): string {
  const relation = relationOf(match);
  const label = relation === "same" || relation === "resized" ? "Same picture" : relation === "cropped" ? "Cropped" : relation === "edited" ? "Edited" : relation === "template" ? "Same template" : "Similar";
  return card(match.id, picture(match.thumb_url ?? undefined, match.image_url ?? undefined, `${label} copy on ${domainOf(match)}`), esc(label), domainOf(match), "Compare with your picture");
}

function origin(family: Family, members: Map<string, FamilyMember>): string {
  const source = family.nodes.find((node) => node.kind === "source");
  const member = source ? members.get(source.members[0]) : undefined;
  const milestones = family.nodes.filter((node) => node.kind === "milestone");
  const entry = family.sources[0];
  const entryUrl = pageUrl(entry?.url);
  if (!member && !milestones.length) return "";
  return `<section class="st-sec" aria-labelledby="st-origin">
    <h2 id="st-origin">Original${entryUrl ? ` <a href="${esc(entryUrl)}" target="_blank" rel="noopener noreferrer">${esc(entry!.source)} ↗</a>` : ""}</h2>
    <div class="st-origin">
      ${member ? card(member.id, memberPicture(member, "The photo with nothing added"), "", memberDomain(member), "Compare the original with your picture") : ""}
      ${milestones.length ? `<ol class="st-time">${milestones.slice(0, 4).map((node) => `<li title="${esc(node.label)}"><b>${esc(dateLabel(node.date))}</b><span>${esc(short(node.label))}</span></li>`).join("")}</ol>` : ""}
    </div>
  </section>`;
}

function spread(trace: TraceCase & { lookup: LookupResult }): string {
  if (trace.lookup.state === "running") return "";
  if (!hasVerdict(trace)) return `<p class="st-call"><b>No campaign found</b> Bluesky, last 48 h</p>`;
  // the watched group whose posts carry this text: the same one the verdict was taken from
  const applying = trace.lookup.clusters.filter((cluster) => cluster.verdict_applies);
  const hit = applying.find((cluster) => cluster.status === trace.status) ?? applying[0];
  const v = STATUS[trace.status];
  const what = trace.status === "coordinated" && hit?.size ? `${hit.size} accounts pushing this text` : v.tag;
  return hit
    ? `<a class="st-call st-hit" href="/?cluster=${encodeURIComponent(String(hit.id))}" style="--vc:${v.css}"><b>${esc(v.word)}</b> ${esc(what)}<span>See the group →</span></a>`
    : `<p class="st-call" style="--vc:${v.css}"><b>${esc(v.word)}</b> ${esc(v.tag)}</p>`;
}

/** What a picture trace found, worked out once for the result page and for the map's panel. */
function summary(trace: TraceCase & { lookup: LookupResult }) {
  const lookup = trace.lookup;
  // a tree with nothing in it but the reader's own picture is not a family: nothing was found, and
  // the picture search's own finding ("found nowhere else") is the answer
  const tree = familyOf(lookup);
  const family = tree && (tree.template || tree.members.some((member) => member.id !== "submitted")) ? tree : null;
  const members = new Map((family?.members ?? []).map((member) => [member.id, member]));
  const variants = (family?.nodes ?? []).filter((node) => node.kind === "variant" && members.has(node.members[0]))
    // dated versions first, oldest first: the nearest thing to an order we can show
    .sort((a, b) => (a.date ? 0 : 1) - (b.date ? 0 : 1) || String(a.date).localeCompare(String(b.date)));
  const matches = imageTraceState(lookup).matches.filter((match) => relationOf(match) !== "different");
  const versions = family ? variants.length : matches.length;
  // without a family, matches the comparison could not confirm are look-alikes, not copies
  const group = family ? "Other versions" : matches.length && matches.every((match) => relationOf(match) === "unknown") ? "Look-alikes" : "Copies";
  return { lookup, family, members, variants, matches, versions, group, h: head(lookup, family) };
}

/** The answer in a few words, and the versions as plain rows: all the map's panel shows of a picture trace. */
export function storyBrief(trace: TraceCase & { lookup: LookupResult }) {
  const { family, members, variants, matches, versions, group, h } = summary(trace);
  const rows = family
    ? variants.map((node) => ({ id: members.get(node.members[0])!.id, title: node.label, sub: memberDomain(members.get(node.members[0])) }))
    : matches.map((match) => ({ id: match.id, title: domainOf(match), sub: "" }));
  return { title: h.title, lead: h.lead, tone: h.tone, group, versions, rows };
}

export function storyHtml(trace: TraceCase & { lookup: LookupResult }, preview?: string): string {
  const { lookup, family, members, variants, matches, group, h } = summary(trace);
  const sub = family?.nodes.find((node) => node.kind === "submitted");
  // the reader's own file while it is at hand: it shows before the upload is answered and never reloads
  const stored = lookup.id === "lookup-pending" ? undefined : comparisonSource(trace);
  const words = (sub?.caption ?? "").split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 6);
  const first = family?.nodes.find((node) => node.kind === "milestone" && node.date);
  const running = lookup.state === "running";
  const facts = [
    first ? ["Since", dateLabel(first.date)] : null,
    sub?.aimed_at ? ["Aimed at", cap(sub.aimed_at)] : null,
  ].filter((fact): fact is string[] => !!fact);
  const grid = family ? variants.map((node) => versionCard(node, members)).join("") : matches.map(matchCard).join("");
  return `<div class="st-wrap">
    <section class="st-hero" data-tone="${h.tone}">
      <figure class="st-mine">${picture(preview ?? stored, preview ? stored : undefined, "The picture you submitted")}</figure>
      <div class="st-say">
        ${family?.template ? `<p class="st-kicker">${esc(family.template)}</p>` : ""}
        <h1>${esc(h.title)}</h1>
        ${h.lead ? `<p class="st-lead">${esc(h.lead)}</p>` : ""}
        ${words.length ? `<p class="st-words">${words.map((word) => `<q>${esc(word)}</q>`).join("")}</p>` : ""}
        ${facts.length ? `<dl class="st-facts">${facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : ""}
        ${running ? progress(lookup) : spread(trace)}
        <div class="st-actions"><button class="btn solid" type="button" data-act="trace">New search</button><button class="btn" type="button" data-story-view="map">Map</button></div>
      </div>
    </section>
    ${family ? origin(family, members) : ""}
    ${grid ? `<section class="st-sec" aria-labelledby="st-versions"><h2 id="st-versions">${group}</h2><div class="st-grid">${grid}</div></section>` : ""}
    ${running ? "" : `<p class="st-fine">Search sample. Dates are upper bounds.</p>`}
  </div>`;
}

/** The story layer over the map. `update` shows it for a picture trace and hides it for anything else. */
export function mountStory(app: HTMLElement) {
  const el = document.createElement("section");
  el.className = "story";
  el.setAttribute("aria-label", "Meme trace result");
  el.hidden = true;
  el.tabIndex = -1;
  app.appendChild(el);
  const back = document.createElement("button");
  back.className = "btn story-back";
  back.type = "button";
  back.dataset.storyView = "story";
  back.textContent = "← Result";
  back.hidden = true;
  app.appendChild(back);
  let shownFor = "";
  let html = "";
  const setView = (view: "story" | "map") => {
    app.dataset.story = view;
    el.hidden = view !== "story";
    back.hidden = view !== "map";
  };
  document.addEventListener("click", (event) => {
    const view = (event.target as HTMLElement).closest<HTMLElement>("[data-story-view]")?.dataset.storyView;
    if (view !== "story" && view !== "map") return;
    setView(view);
    if (view === "story") el.focus();
  });
  // a picture that will not load falls back to its second address, then to a placeholder
  el.addEventListener("error", (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement) || !img.hasAttribute("data-story-img")) return;
    const next = img.dataset.fallback;
    img.dataset.fallback = "";
    if (next) img.src = next;
    else img.replaceWith(Object.assign(document.createElement("span"), { className: "st-noimg", textContent: "No picture" }));
  }, true);
  return {
    update(trace: TraceCase | null, preview?: string) {
      if (!hasStory(trace)) {
        delete app.dataset.story;
        el.hidden = true;
        back.hidden = true;
        shownFor = "";
        html = "";
        el.innerHTML = "";
        return;
      }
      // a new trace opens on its story; an update to the same one keeps the view the reader chose
      const key = trace.lookup.id === "lookup-pending" ? "pending" : trace.lookup.id;
      if (shownFor !== key && !(shownFor === "pending" && app.dataset.story)) { setView("story"); el.scrollTop = 0; }
      shownFor = key;
      const next = storyHtml(trace, preview);
      if (next !== html) { html = next; el.innerHTML = next; }
    },
  };
}
