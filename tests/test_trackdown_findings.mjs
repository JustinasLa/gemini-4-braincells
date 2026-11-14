import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/ui/findings.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { factualFindings, coordinationNote } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`
);

function result() {
  return {
    status: "insufficient",
    subject_id: "post:1",
    clusters: [],
    posts: [],
    media: [],
    receipts: [{ id: 1, source_url: "https://record.example/vote", ok: true }],
    assessment: {
      model: "test-model",
      claim_checks: [{
        claim: "The amendment was defeated 99–1.",
        finding: "supported",
        explanation: "This was a procedural vote, not a guaranteed 50% price reduction.",
        cites: [1],
      }],
    },
  };
}

test("supported facts remain visible when coordination is insufficient", () => {
  const lookup = result();
  const html = factualFindings(lookup);
  assert.match(html, /Supported by cited sources/);
  assert.match(html, /procedural vote/);
  assert.match(html, /href="https:\/\/record.example\/vote"/);
  assert.match(html, /separate from coordination/);
  assert.match(coordinationNote(lookup), /Coordination: insufficient evidence/);
  assert.equal(lookup.status, "insufficient");
});

test("account-only cluster matches never become a coordination verdict", () => {
  const lookup = result();
  lookup.status = "matched";
  lookup.clusters = [{ verdict_applies: false }];
  assert.match(coordinationNote(lookup), /insufficient evidence/);
  lookup.clusters[0].verdict_applies = true;
  assert.match(coordinationNote(lookup), /does not establish whether the content is true or false/);
});

test("unavailable and legacy assessments do not imply false content", () => {
  for (const assessment of [null, { model: "legacy" }, { model: "new", claim_checks: [] }]) {
    const html = factualFindings({ ...result(), assessment });
    assert.match(html, /No claim-level factual assessment is available/);
    assert.match(html, /not evidence that the content is false/);
    assert.doesNotMatch(html, /Supported by cited sources/);
  }
});

test("model text is escaped and unsafe, failed, or invented citations cannot become links", () => {
  const lookup = result();
  lookup.assessment.claim_checks[0].claim = '<img src=x onerror="alert(1)">';
  lookup.assessment.claim_checks[0].explanation = "<script>bad()</script>";
  lookup.assessment.claim_checks[0].cites = [1, 2, 3, 999];
  lookup.receipts = [
    { id: 1, source_url: "javascript:alert(1)" },
    { id: 2, source_url: "data:text/html,unsafe" },
    { id: 3, source_url: "https://failed.example", ok: false },
  ];
  const html = factualFindings(lookup);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|<img|href=|Receipt #999|failed.example/);
});

test("image-only inputs and legacy media readings retain a provenance warning", () => {
  const lookup = result();
  lookup.posts = [{ id: "post:1", media_urls: ["https://image.example/photo.jpg"] }];
  const html = factualFindings(lookup);
  assert.match(html, /Source discovery and exact-image verification are separate/);
  assert.match(html, /OCR alone do not authenticate/);
  assert.match(html, /not necessarily the original source/);
});

test("all factual finding labels are distinct from coordination", () => {
  for (const [finding, label] of Object.entries({
    supported: "Supported by cited sources",
    contradicted: "Contradicted by cited sources",
    mixed: "Partly supported / needs context",
    unverified: "Not independently verified",
  })) {
    const lookup = result();
    lookup.assessment.claim_checks[0].finding = finding;
    assert.ok(factualFindings(lookup).includes(label));
  }
});

test("both models retain their attributed claim-level findings", () => {
  const lookup = result();
  lookup.readings = [lookup.assessment, {
    model: "second-model",
    claim_checks: [{
      claim: "The second reading distinguishes the proposed saving from a result.",
      finding: "mixed",
      explanation: "The source reports an estimate.",
      cites: [1],
    }],
  }];
  const html = factualFindings(lookup);
  assert.match(html, /Model assessment · test-model/);
  assert.match(html, /Model assessment · second-model/);
  assert.match(html, /Supported by cited sources/);
  assert.match(html, /Partly supported \/ needs context/);
  assert.match(html, /The source reports an estimate/);
});

test("source articles remain visible when the model times out", () => {
  const lookup = result();
  lookup.assessment = null;
  lookup.pages = [{
    url: "https://www.businessinsider.com/example-photo-source",
    headline: "Contemporary photo coverage",
    published_hint: "2022-08-08",
    receipt_id: 1,
  }];
  let html = factualFindings(lookup);
  assert.match(html, /Contemporary photo coverage/);
  assert.match(html, /Search lead · page not read/);
  assert.match(html, /Search date hint: 2022-08-08/);
  assert.match(html, /No claim-level factual assessment/);
  lookup.receipts[0].endpoint = "/scrape";
  lookup.pages[0].published_at = "2022-08-08";
  html = factualFindings(lookup);
  assert.match(html, /Source page read · not automatic verification/);
  assert.match(html, /Published: 2022-08-08/);
});

test("source attribution is shown with its receipt rather than a blanket denial", () => {
  const lookup = result();
  lookup.assessment.media_provenance = "The cited article credits the photographer.";
  lookup.assessment.media_provenance_cites = [1];
  const html = factualFindings(lookup);
  assert.match(html, /The cited article credits the photographer/);
  assert.match(html, /Receipt #1/);
  assert.doesNotMatch(html, /Exact media provenance is unverified/);
});

test("collected source page metadata is escaped and unsafe URLs are omitted", () => {
  const lookup = result();
  lookup.pages = [
    {url: "javascript:alert(1)", headline: "Bad"},
    {url: "https://source.example", headline: "<script>bad()</script>", published_hint: "<img>"},
  ];
  const html = factualFindings(lookup);
  assert.doesNotMatch(html, /javascript:|<script>|<img>/);
  assert.match(html, /&lt;script&gt;/);
});
