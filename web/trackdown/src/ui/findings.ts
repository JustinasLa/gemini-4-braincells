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
    ? "Coordination status comes from the matched monitored cluster. It does not establish whether the content is true or false."
    : "Coordination: insufficient evidence. Repeated wording and engagement alone do not establish organized amplification, or prove organic sharing. This does not mean the content is false or unreadable.";
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
    const role = page.role === "reference" ? "Text-copy candidate" : page.role === "linked" ? "Linked page" : "Coverage candidate";
    const date = read && page.published_at
      ? ` · Published: ${page.published_at}`
      : page.published_hint ? ` · Search date hint: ${page.published_hint}` : "";
    return [`<section class="data-group"><a class="src" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(page.headline || new URL(url).hostname)} ↗</a><div class="receipt-meta">${role} · ${read ? "Source page read · not automatic verification" : "Search lead · page not read"}${esc(date)}</div></section>`];
  }).join("");
  const mediaNote = media && !readings.some((reading) => reading.media_provenance)
    ? '<div class="notice"><b>Media attribution.</b> Source discovery and exact-image verification are separate. Collected articles may document attribution; image descriptions and OCR alone do not authenticate the image or its caption.</div>'
    : "";
  const unavailable = '<p class="lookup-message">No claim-level factual assessment is available. This is not evidence that the content is false.</p>';
  const findings = readings.map((reading) => {
    const checks = reading.claim_checks ?? [];
    return `<div class="receipt-meta">Model assessment · ${esc(reading.model)} · based on the collected sources, not a guarantee of truth</div>
      ${checks.length ? checks.map((check) => `<section class="data-group"><h4>${esc(check.claim)}</h4><div class="label">${esc(FINDING[check.finding] ?? FINDING.unverified)}</div><p class="lookup-message">${esc(check.explanation)}</p>${sources(check.cites)}</section>`).join("") : unavailable}
      ${reading.media_provenance ? `<div class="notice"><b>Media attribution.</b> ${esc(reading.media_provenance)}${sources(reading.media_provenance_cites ?? [])}</div>` : ""}`;
  }).join("");
  return `<section aria-label="Factual findings"><div class="sect label" style="margin-top:0">Factual findings · separate from coordination</div>
    ${findings || unavailable}
    ${mediaNote}
    ${sourcePages ? `<div class="sect label">Collected source pages · available even without a model reading</div>${sourcePages}` : ""}
    <p class="lookup-message">The earliest observed copy is only the earliest found in this search sample, not necessarily the original source.</p></section>`;
}
