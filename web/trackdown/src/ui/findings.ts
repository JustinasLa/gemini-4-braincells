import type { ClaimCheck, LookupResult } from "../data/lookup";
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
  const assessment = result.assessment;
  const checks = assessment?.claim_checks ?? [];
  const receipts = new Map(result.receipts.filter((receipt) => receipt.ok !== false).map((receipt) => [receipt.id, receipt]));
  const sources = (check: ClaimCheck) => [...new Set(check.cites)].flatMap((id) => {
    const receipt = receipts.get(id);
    if (!receipt) return [];
    const url = httpUrl(receipt.source_url);
    return [url
      ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Receipt #${id} · ${esc(new URL(url).hostname)} ↗</a>`
      : `<span class="receipt-meta">Receipt #${id}</span>`];
  }).join("");
  const media = result.media.length > 0 || result.posts.some((post) => post.id === result.subject_id && post.media_urls.length > 0);
  const provenance = assessment?.media_provenance || (media
    ? "Exact media provenance is unverified. Image descriptions and OCR do not authenticate the image, its date, location, creator, or the caption's explanation."
    : "");
  return `<section aria-label="Factual findings"><div class="sect label" style="margin-top:0">Factual findings · separate from coordination</div>
    ${checks.length ? checks.map((check) => `<section class="data-group"><h4>${esc(check.claim)}</h4><div class="label">${esc(FINDING[check.finding] ?? FINDING.unverified)}</div><p class="lookup-message">${esc(check.explanation)}</p>${sources(check)}</section>`).join("") : '<p class="lookup-message">No claim-level factual assessment is available. This is not evidence that the content is false.</p>'}
    ${assessment ? `<div class="receipt-meta">Model assessment · ${esc(assessment.model)} · based on the collected sources, not a guarantee of truth</div>` : ""}
    ${provenance ? `<div class="notice"><b>Media provenance.</b> ${esc(provenance)}</div>` : ""}
    <p class="lookup-message">The earliest observed copy is only the earliest found in this search sample, not necessarily the original source.</p></section>`;
}
