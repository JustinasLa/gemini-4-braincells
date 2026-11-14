import { domainOf, firstSeenLabel, imageTraceState, pageUrl, relationOf } from "../data/imageTrace";
import type { TraceCase, TraceNode } from "../types";
import { esc } from "./format";
import { memeImage, installImageFallbacks } from "./memeImage";
import { openCompare, refreshComparison } from "./compare";
import { hasStory, storyBrief } from "./story";
import "./meme.css";

export function imageCardHtml(node: TraceNode): string {
  const match = node.imageMatch!;
  const relation = relationOf(match);
  const url = pageUrl(match.page_url);
  return `<div class="meme-card meme-${relation}" data-ui>
    <button class="meme-card-open" data-ui data-meme-compare="${esc(node.id)}" aria-label="Compare ${esc(relation)} copy on ${esc(domainOf(match))}">
      <span class="meme-eyebrow">${node.eyebrow ? esc(node.eyebrow) : node.role === "origin" ? "ORIGINAL · EARLIEST MATCH FOUND" : relation === "template" ? "SAME TEMPLATE · OTHER CONTENT" : `${esc(relation.toUpperCase())} · VARIATION`}</span>
      <strong>${esc(domainOf(match))}</strong>
      <span class="meme-card-preview">Compare with submission ↗</span>
    </button>
    ${memeImage(match, true)}
    <p class="meme-date">${esc(firstSeenLabel(match))}</p>
    ${url ? `<a data-ui href="${esc(url)}" target="_blank" rel="noopener noreferrer">Source page ↗</a>` : ""}
  </div>`;
}

export function mountVariations(panel: HTMLElement, trace: TraceCase, tab: string) {
  refreshComparison(trace);
  const state = trace.lookup ? imageTraceState(trace.lookup) : null;
  panel.closest(".app")?.classList.toggle("meme-active", !!state?.enabled);
  const peek = document.querySelector<HTMLElement>("#peek");
  if (peek) peek.setAttribute("aria-label", state?.enabled ? "Show meme trace and evidence" : "Show verdict and evidence");
  if (!state?.enabled) return;
  // the phone verdict bar shows the image search's progress whichever panel tab is open
  if (peek && (state.searching || state.comparing)) peek.innerHTML = `<span class="pk-t"><b>${state.searching ? "Searching for earlier copies…" : "Comparing variations…"}</b><span>${trace.mock ? "MOCK · " : ""}Meme trace · view progress</span></span><span class="pk-go">Open ↑</span>`;
  if (tab !== "verdict" || !hasStory(trace)) return;
  // the result page tells the story; the map's panel keeps a headline and the versions to compare
  const brief = storyBrief(trace);
  const row = (item: (typeof brief.rows)[number]) => `<button class="meme-list-item" data-meme-compare="${esc(item.id)}"><span>${esc(item.title)}</span>${item.sub ? `<small>${esc(item.sub)}</small>` : ""}<span class="meme-list-arrow" aria-hidden="true">↗</span></button>`;
  const SHOWN = 4;
  const html = `<section class="meme-section" aria-label="Meme trace">
    <div class="meme-brief" data-tone="${brief.tone}">${esc(brief.title)}</div>
    ${brief.lead ? `<p class="meme-note">${esc(brief.lead)}</p>` : ""}
    ${brief.rows.length ? `<h3>${esc(brief.group)} <span class="meme-count">${brief.versions}</span></h3>${brief.rows.slice(0, SHOWN).map(row).join("")}${brief.rows.length > SHOWN ? `<details class="meme-different"><summary>${brief.rows.length - SHOWN} more</summary>${brief.rows.slice(SHOWN).map(row).join("")}</details>` : ""}` : ""}
  </section>`;
  panel.querySelector(".pane")?.insertAdjacentHTML("afterbegin", html);
}

export function initMemeTrace(current: () => TraceCase | null) {
  installImageFallbacks();
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const opener = target?.closest<HTMLElement>("[data-meme-compare]");
    const card = target?.closest<HTMLElement>(".card:has(.meme-card)");
    if (!opener && (!card || target?.closest("a"))) return;
    const trace = current();
    const id = opener?.dataset.memeCompare ?? card?.dataset.id;
    // a family tree card that is not an image match carries its own picture (data/family.ts)
    const match = trace?.lookup?.image_matches?.find((candidate) => candidate.id === id) ?? trace?.nodes.find((node) => node.id === id)?.imageMatch;
    if (trace && match) openCompare(trace, match, opener ?? card!.querySelector<HTMLElement>("button")!);
  });
  document.querySelector("#legend .legend-hint")?.insertAdjacentHTML("beforebegin", '<div class="li"><span class="ln" style="--s:dashed;--c:#a2ffb5"></span>Image variation</div>');
}
