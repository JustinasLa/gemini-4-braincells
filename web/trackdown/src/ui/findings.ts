import type { ClaimCheck, LookupResult } from "../data/lookup";
import { readingsOf } from "../data/lookup";
import { esc, httpUrl } from "./format";

const FINDING: Record<ClaimCheck["finding"], string> = {
  supported: "Supported by cited sources",
  contradicted: "Contradicted by cited sources",
  mixed: "Partly supported / needs context",
  unverified: "Not independently verified",
};

export function coordinationNote(result: LookupResult): string {
  const matched = result.status === "matched" && result.clusters.some((cluster) => cluster.verdict_applies);
  return matched
    ? "From the matched cluster. It does not establish whether the content is true or false."
    : "Coordination: insufficient evidence. Not a call on whether the content is true.";
}

export function factualFindings(result: LookupResult): string {
  const readings = readingsOf(result);
  const receipts = new Map(result.receipts.filter((receipt) => receipt.ok !== false).map((receipt) => [receipt.id, receipt]));
  const sources = (cites: number[]) => [...new Set(cites)].flatMap((id) => {
    const receipt = receipts.get(id);
    if (!receipt) return [];
    const url = httpUrl(receipt.source_url);
    return [url
      ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Receipt #${id} · ${esc(new URL(url).hostname)} ↗</a>`
      : `<span class="receipt-meta">Receipt #${id}</span>`];
  }).join("");
  const media = result.media.length > 0 || result.posts.some((post) => post.id === result.subject_id && post.media_urls.length > 0);
  const sourcePages = (result.pages ?? []).flatMap((page) => {
    const url = httpUrl(page.final_url || page.url);
    if (!url) return [];
    const receipt = page.receipt_id == null ? null : receipts.get(page.receipt_id);
    const read = receipt?.endpoint === "/scrape";
    const role = page.role === "reference" ? "Text copy" : page.role === "linked" ? "Linked page" : "Coverage";
    // a date read on the page is stated; a search engine's date is marked as approximate
    const date = read && page.published_at
      ? ` · ${page.published_at.slice(0, 10)}`
      : page.published_hint ? ` · ~${page.published_hint}` : "";
    return [`<section class="data-group"><a class="src" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(page.headline || new URL(url).hostname)} ↗</a><div class="receipt-meta">${role} · ${read ? "page read" : "search lead"}${esc(date)}</div></section>`];
  });
  // the first few sources are enough at a glance; the rest fold away
  const SHOWN = 4;
  const sourceList = sourcePages.slice(0, SHOWN).join("") + (sourcePages.length > SHOWN ? `<details class="more-sources"><summary>${sourcePages.length - SHOWN} more</summary>${sourcePages.slice(SHOWN).join("")}</details>` : "");
  const mediaNote = media && !readings.some((reading) => reading.media_provenance)
    ? '<div class="notice"><b>Media.</b> Finding a source is not verifying the image.</div>'
    : "";
  const findings = readings.map((reading) => {
    const checks = reading.claim_checks ?? [];
    return `${checks.length ? checks.map((check) => `<section class="data-group"><h4>${esc(check.claim)}</h4><div class="label">${esc(FINDING[check.finding] ?? FINDING.unverified)}</div><p class="lookup-message">${esc(check.explanation)}</p>${sources(check.cites)}</section>`).join("") : ""}
      ${reading.media_provenance ? `<div class="notice"><b>Media.</b> ${esc(reading.media_provenance)}${sources(reading.media_provenance_cites ?? [])}</div>` : ""}`;
  }).join("");
  return `<section aria-label="Factual findings">
    ${findings}
    ${mediaNote}
    ${sourceList}
    <p class="lookup-message">Earliest found, not proof of the original.</p></section>`;
}
