import { domainOf, firstSeenLabel, humanLabel, imageTraceState, memeFinding, pageUrl, relationOf } from "../data/imageTrace";
import type { TraceCase, TraceNode } from "../types";
import { esc } from "./format";
import { memeImage, installImageFallbacks } from "./memeImage";
import { openCompare, refreshComparison } from "./compare";
import "./meme.css";

export function imageCardHtml(node: TraceNode): string {
  const match = node.imageMatch!;
  const relation = relationOf(match);
  const url = pageUrl(match.page_url);
  return `<div class="meme-card meme-${relation}" data-ui>
    <button class="meme-card-open" data-ui data-meme-compare="${esc(node.id)}" aria-label="Compare ${esc(relation)} copy on ${esc(domainOf(match))}">
      <span class="meme-eyebrow">// ${node.role === "origin" ? "ORIGINAL · EARLIEST MATCH FOUND" : `${esc(relation.toUpperCase())} · VARIATION`}</span>
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
  if (!["verdict", "receipts"].includes(tab)) return;
  const count = state.groups.reduce((total, group) => total + group.matches.length, 0);
  const original = state.origin;
  const finding = trace.lookup ? memeFinding(trace.lookup) : null;
  const button = (id: string, title: string, subtitle: string) => `<button class="meme-list-item" data-meme-compare="${esc(id)}"><span>${esc(title)}</span><small>${esc(subtitle)}</small><span class="meme-list-arrow" aria-hidden="true">↗</span></button>`;
  const html = `<section class="meme-section" aria-label="Meme trace">
    <h2 class="meme-eyebrow">// ${trace.mock ? "MOCK · " : ""}MEME TRACE</h2>
    ${finding && finding !== "pending" ? `<div class="meme-finding" style="--vc:${finding.color}"><div class="label">What the image search found · not a coordination call</div><div class="meme-finding-word">${esc(finding.word.toUpperCase())}</div><p>${esc(finding.sentence)}</p></div>` : ""}
    <h3>Original <small>earliest match found</small></h3>
    ${state.searching ? '<p class="meme-scanning" role="status">Searching for earlier copies…</p>' : ""}
    ${original ? `${button(original.id, domainOf(original), firstSeenLabel(original))}<p class="meme-note">Earliest match returned, not proof of the first publication or authorship.</p>` : state.noEarlier ? '<p class="meme-empty">No earlier copy found</p>' : !state.searching ? '<p class="meme-note">No original established.</p>' : ""}
    ${state.failures.map((failure) => `<p class="meme-failure" role="status">${esc(failure)}</p>`).join("")}
    ${state.skipped.map((note) => `<p class="meme-note">${esc(note)}</p>`).join("")}
    <h3>Variations <span class="meme-count">${count}</span></h3>
    ${state.comparing ? '<p class="meme-scanning" role="status">Comparing variations…</p>' : ""}
    ${state.groups.filter((group) => group.matches.length).map((group) => `<div class="meme-group meme-${group.relation}"><h4>${humanLabel(group.relation)} <span>${group.matches.length}</span></h4>${group.matches.map((match) => button(match.id, domainOf(match), match.relation_note || firstSeenLabel(match))).join("")}</div>`).join("")}
    ${!count && !state.comparing && !state.searching ? '<p class="meme-note">No variations returned.</p>' : ""}
    ${state.different.length ? `<details class="meme-different"><summary>Looks different (not counted) · ${state.different.length}</summary>${state.different.map((match) => button(match.id, domainOf(match), "Looks different · not a variation")).join("")}</details>` : ""}
    <p class="meme-note">Image matches are a search sample. Connections show similarity, not who copied whom.</p>
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
    const match = trace?.lookup?.image_matches?.find((candidate) => candidate.id === id);
    if (trace && match) openCompare(trace, match, opener ?? card!.querySelector<HTMLElement>("button")!);
  });
  document.querySelector("#legend .legend-hint")?.insertAdjacentHTML("beforebegin", '<div class="li"><span class="ln" style="--s:dashed;--c:#a2ffb5"></span>Image variation</div>');
}
