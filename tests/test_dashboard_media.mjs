import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { mediaEvidenceItems, renderMediaEvidence, safeHttpsUrl } from "../web/public/media-evidence.mjs";

const formatDate = (value) => `date:${value}`;
const mediaRow = (hash, detail = {}, extra = {}) => ({
  id: 1,
  kind: "media",
  summary: "Evidence summary.",
  detail: {
    status: "complete",
    media_hash: hash,
    description: "A repeated campaign graphic.",
    media_type: "image",
    extracted_text: ["MEETING FRIDAY"],
    model: "nemotron-3.5-super-vl",
    confidence: 0.82,
    ...detail,
  },
  model: "nemotron-3.5-super-vl",
  source_url: "https://bsky.app/profile/did:example/post/1",
  created_at: "2026-11-14T01:00:00Z",
  ...extra,
});

test("complete media shows assessment, OCR, attribution, time, and safe links", () => {
  const record = mediaRow("cid-one", {
    media_url: "https://cdn.bsky.app/img/blob/cid-one",
    manipulation_cues: ["Text overlay is unusually uniform"],
    reuse_cues: ["Same image appears across accounts"],
  });
  const [item] = mediaEvidenceItems([{ media_hashes: ["cid-one"] }], [record]);
  const html = renderMediaEvidence(item, formatDate);

  assert.equal(item.status, "complete");
  assert.match(html, /Nemotron assessment/);
  assert.match(html, /MEETING FRIDAY/);
  assert.match(html, /Media type:<\/strong> image/);
  assert.match(html, /Model\/provider: NVIDIA \/ nemotron-3.5-super-vl/);
  assert.match(html, /date:2026-11-14T01:00:00Z/);
  assert.match(html, /Open media preview/);
  assert.match(html, /Open source post/);
  assert.match(html, /Reuse cues/);
  assert.match(html, /Context cues/);
});

test("CID grouping deduplicates repeated posts and keeps OCR-template evidence", () => {
  const evidence = [
    mediaRow("cid-one"),
    mediaRow("cid-two", { media_type: "video" }),
    {
      kind: "media",
      summary: "Two images reuse an OCR template.",
      detail: {
        status: "complete",
        type: "ocr_template",
        media_hash: "ocr-template",
        media_hashes: ["cid-one", "cid-two"],
        shared_tokens: ["USE", "VIDEO"],
        varying_tokens: ["AUSTIN", "CHICAGO"],
      },
      model: "nemotron-3.5-super-vl",
      created_at: "2026-11-14T01:01:00Z",
    },
  ];
  const items = mediaEvidenceItems(
    [{ media_hashes: ["cid-one", "cid-two", "cid-one"] }],
    evidence,
  );

  assert.equal(items.length, 3);
  const template = items.find((item) => item.detail.type === "ocr_template");
  const html = renderMediaEvidence(template, formatDate);
  assert.match(html, /OCR template/);
  assert.match(html, /Nemotron assessment/);
  assert.match(html, /USE VIDEO/);
  assert.match(html, /AUSTIN, CHICAGO/);
});

test("missing and pending media are explicit and never look like analysis", () => {
  const [missing] = mediaEvidenceItems([], []);
  const missingHtml = renderMediaEvidence(missing, formatDate);
  assert.equal(missing.status, "missing");
  assert.match(missingHtml, /No media hashes or media evidence/);

  const [pending] = mediaEvidenceItems(
    [{ media_hashes: ["cid-pending"], url: "https://bsky.app/profile/did:x/post/2" }],
    [],
  );
  const pendingHtml = renderMediaEvidence(pending, formatDate);
  assert.equal(pending.status, "pending");
  assert.match(pendingHtml, /No analysis result is stored/);
  assert.match(pendingHtml, /This is not a media finding/);
  assert.doesNotMatch(pendingHtml, /Nemotron assessment/);
});

test("failed and malformed evidence do not display success-shaped fallbacks", () => {
  const failed = {
    kind: "media",
    summary: "private provider response must not be shown",
    detail: {
      status: "failed",
      media_hash: "cid-failed",
      stage: "analysis",
      error_code: "NemotronError",
      error: "private provider response must not be shown",
    },
    source_url: "https://bsky.app/profile/did:x/post/3",
  };
  const [failedItem] = mediaEvidenceItems([], [failed]);
  const failedHtml = renderMediaEvidence(failedItem, formatDate);
  assert.equal(failedItem.status, "failed");
  assert.match(failedHtml, /Processing failed during analysis/);
  assert.doesNotMatch(failedHtml, /private provider response/);
  assert.doesNotMatch(failedHtml, /Nemotron assessment/);

  const malformed = { kind: "media", summary: "fake success", detail: null };
  const [malformedItem] = mediaEvidenceItems([], [malformed]);
  const malformedHtml = renderMediaEvidence(malformedItem, formatDate);
  assert.equal(malformedItem.status, "malformed");
  assert.match(malformedHtml, /incomplete or malformed/);
  assert.doesNotMatch(malformedHtml, /fake success/);
  assert.doesNotMatch(malformedHtml, /Nemotron assessment/);
});

test("unsafe links and unescaped model text are never rendered as active HTML", () => {
  const record = mediaRow("cid-unsafe", {
    description: '<img src=x onerror="alert(1)">',
    media_url: "javascript:alert(1)",
  }, {
    source_url: "data:text/html,alert(1)",
    model: null,
  });
  const [item] = mediaEvidenceItems([{ media_hashes: ["cid-unsafe"] }], [record]);
  const html = renderMediaEvidence(item, formatDate);

  assert.match(html, /&lt;img/);
  assert.match(html, /Model\/provider: NVIDIA \/ nemotron-3.5-super-vl/);
  assert.match(html, /URL is not a safe HTTPS link/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.doesNotMatch(html, /href="data:/);
  assert.equal(safeHttpsUrl("https://user:pass@example.test/media"), null);
  assert.equal(safeHttpsUrl("http://example.test/media"), null);
});

test("dashboard imports and uses the tested media renderer", () => {
  const html = readFileSync(new URL("../web/public/monitor/index.html", import.meta.url), "utf8");
  assert.match(html, /<script type="module">/);
  assert.match(html, /from "\.\.\/media-evidence\.mjs"/);
  assert.match(html, /mediaEvidenceItems\(c\.posts, evidence\)/);
});
