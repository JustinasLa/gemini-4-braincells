import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/ui/card.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
  loader: { ".webp": "empty", ".css": "empty" },
});
const { cardHtml } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);

const { outputFiles: imageFiles } = await build({
  stdin: {
    contents: 'export * from "./src/data/imageTrace.ts"; export { lookupCase } from "./src/data/lookup.ts"; export { memeFixture } from "./src/data/mockMeme.ts";',
    resolveDir: fileURLToPath(new URL("../web/trackdown/", import.meta.url)),
    loader: "ts",
  },
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { firstSeenLabel, latestImageTime, imageTraceState, memeFinding, lookupCase, memeFixture } = await import(
  `data:text/javascript;base64,${Buffer.from(imageFiles[0].text).toString("base64")}`
);

function imageLookup(overrides) {
  const result = memeFixture(); // submitted at 2026-11-14 12:00 UTC
  result.image_matches = [{ ...result.image_matches[0], ...overrides }];
  return result;
}

const matchNode = (trace) => trace.nodes.find((node) => node.imageMatch);

const post = {
  id: "test-post",
  role: "submitted",
  platform: "bluesky",
  author: { name: "Example", handle: "example.test", ageDays: 90 },
  text: "Example post",
  t: Date.UTC(2026, 10, 14),
  flags: ["satire label dropped"],
};

test("submitted posts retain a plain role label without the external stamp", () => {
  const html = cardHtml(post, false, 1);
  assert.match(html, /class="rolemark">Submitted<\/div>/);
  assert.doesNotMatch(html, /class="stamp"|SUBMITTED/);
  assert.match(html, /satire label dropped/);
  assert.match(html, /Example post/);
});

test("plain card labels preserve provenance distinctions", () => {
  for (const [role, label] of [
    ["origin", "Earliest known"],
    ["mutation", "Altered copy"],
    ["source", "Found by search"],
    ["screenshotted", "Real post in the screenshot"],
  ]) {
    const html = cardHtml({ ...post, role }, false, 1);
    assert.ok(html.includes(`<div class="rolemark">${label}</div>`));
    assert.doesNotMatch(html, /class="stamp"/);
  }
});

test("post-id dates appear in image cards, flags, facts and finding labels without crawl slack", () => {
  const result = imageLookup({ time_source: "snowflake", first_seen: "2026-11-14T11:59:59Z", time_uncertainty_s: 0, page_url: "https://x.com/example/status/1990000000000000000" });
  const label = "time from post id · 2026-11-14 11:59:59 UTC";
  const node = matchNode(lookupCase(result));
  assert.equal(firstSeenLabel(result.image_matches[0]), label);
  assert.deepEqual(node.flags, [label]);
  assert.equal(node.realData[0].facts[0].value, label);
  assert.ok(cardHtml(node, false, 1).includes(label));
  const finding = memeFinding(result);
  assert.equal(finding.kind, "copy");
  assert.ok(finding.tag.endsWith("· 2026-11-14"), finding.tag); // a short tag; the sentence has the time
  assert.ok(finding.sentence.includes(label));
});

test("date-only pages need their entire day to precede the submitted post", () => {
  for (const [date, kind, ordered] of [["2026-11-14", "original", false], ["2026-11-13", "copy", true]]) {
    const result = imageLookup({ time_source: "page", first_seen: date, time_uncertainty_s: 86400 });
    const finding = memeFinding(result);
    const trace = lookupCase(result);
    const node = matchNode(trace);
    assert.equal(finding.kind, kind, date);
    assert.equal(trace.originId, ordered ? node.id : "");
    assert.equal(trace.edges.find((edge) => edge.to === result.subject_id).rel, ordered ? "media" : "variant");
    assert.equal(node.undated, false, "a page has a publication date even when its clock time is uncertain");
    assert.deepEqual(node.flags, [`page dated ${date}`]);
    assert.equal(node.realData[0].facts[0].value, `page dated ${date}`);
    assert.ok(cardHtml(node, false, 1).includes(`page dated ${date}`));
    if (ordered) {
      assert.ok(finding.tag.includes(`page dated ${date}`));
      assert.ok(finding.sentence.includes(`page dated ${date}`));
    }
  }
});

test("page windows in an unknown zone use 28 hours and a strict before boundary", () => {
  const result = imageLookup({ time_source: "page", first_seen: "2026-11-13T08:00:00Z", time_uncertainty_s: 100800 });
  assert.equal(latestImageTime(result.image_matches[0]), Date.parse(result.posts[0].created_at));
  assert.equal(memeFinding(result).kind, "original");
  assert.equal(lookupCase(result).originId, "");
  result.image_matches[0].first_seen = "2026-11-13T07:59:59Z";
  assert.equal(memeFinding(result).kind, "copy");
  assert.equal(lookupCase(result).originId, "img:origin");
});

test("an uncertain page is after the post only when its earliest time is after it", () => {
  const result = imageLookup({ time_source: "page", first_seen: "2026-11-14T11:00:00Z", time_uncertainty_s: 100800 });
  assert.equal(imageTraceState(result).origin?.id, "img:origin", "overlapping windows do not prove it is later");
  assert.equal(lookupCase(result).originId, "", "overlapping windows do not establish lineage either");
  result.image_matches[0].first_seen = "2026-11-14T12:00:01Z";
  assert.equal(imageTraceState(result).origin, undefined);
  assert.equal(matchNode(lookupCase(result)).role, "source");
});

test("crawl dates retain default day slack, labels and undated timeline behavior", () => {
  for (const uncertainty of [undefined, null, 86400]) {
    for (const [first_seen, kind] of [["2026-11-14T00:00:00Z", "original"], ["2026-11-13T12:00:00Z", "original"], ["2026-11-13T11:59:59Z", "copy"]]) {
      const result = imageLookup({ first_seen, time_source: "crawl_date", time_uncertainty_s: uncertainty });
      const finding = memeFinding(result);
      const trace = lookupCase(result);
      assert.equal(finding.kind, kind);
      assert.equal(trace.originId, "");
      assert.equal(matchNode(trace).undated, true);
      assert.match(firstSeenLabel(result.image_matches[0]), /first seen by TinEye on .*upper bound, not a publish date/);
      if (kind === "copy") assert.match(finding.tag, /seen by 2026-11-13$/);
    }
  }
});

test("exact and legacy page/post-id dates get no implicit slack, including explicit zero", () => {
  for (const time_source of ["page", "snowflake", "crawl_date"]) {
    for (const uncertainty of time_source === "crawl_date" ? [0] : [undefined, null, 0]) {
      const result = imageLookup({ time_source, first_seen: "2026-11-14T11:59:59Z", time_uncertainty_s: uncertainty });
      assert.equal(memeFinding(result).kind, "copy");
      if (time_source === "page") assert.equal(firstSeenLabel(result.image_matches[0]), "page dated 2026-11-14");
    }
  }
});

test("date receipts have their own group and preserve the search receipt group", () => {
  for (const time_source of ["page", "snowflake"]) {
    const result = imageLookup({ time_source });
    const originalGroup = matchNode(lookupCase(result)).realData[0];
    for (const time_receipt_id of [0, 37]) {
      result.image_matches[0].time_receipt_id = time_receipt_id;
      const groups = matchNode(lookupCase(result)).realData;
      assert.equal(groups.length, 2);
      assert.deepEqual(groups[0], originalGroup);
      assert.deepEqual(groups[1], {
        label: "Image date", url: result.image_matches[0].page_url, receiptId: time_receipt_id,
        facts: [{ label: "Image date", value: firstSeenLabel(result.image_matches[0]) }],
      });
    }
    result.image_matches[0].time_receipt_id = null;
    assert.deepEqual(matchNode(lookupCase(result)).realData, [originalGroup]);
  }
});

test("findings order older copies by their latest possible time, like the server's origin", () => {
  const result = memeFixture(); // submitted at 2026-11-14 12:00 UTC
  const [first] = result.image_matches;
  result.image_matches = [
    { ...first, id: "img:page", relation: "edited", time_source: "page", first_seen: "2026-11-13T00:00:00Z", time_uncertainty_s: 86400, changes: ["Caption changed."], changes_from: "match" },
    { ...first, id: "img:post", relation: "same", time_source: "snowflake", first_seen: "2026-11-13T10:00:00Z", time_uncertainty_s: null, page_url: "https://x.com/a/status/1990000000000000000" },
  ];
  const finding = memeFinding(result);
  assert.equal(finding.kind, "copy");
  assert.equal(finding.match.id, "img:post");
});

test("a page clock time without a zone is labelled with its printed date, not its window's lower edge", () => {
  // the page printed 2012-05-06T10:00:00 (no zone): the server stores 2012-05-05T20:00Z + 28 h
  const result = imageLookup({ time_source: "page", first_seen: "2012-05-05T20:00:00Z", time_uncertainty_s: 100800 });
  assert.equal(firstSeenLabel(result.image_matches[0]), "page dated 2012-05-06");
  assert.equal(firstSeenLabel({ ...result.image_matches[0], first_seen: "2012-05-06T00:00:00Z", time_uncertainty_s: 86400 }), "page dated 2012-05-06");
});

test("a copy whose date window overlaps the post is not called later", () => {
  const result = imageLookup({ time_source: "page", first_seen: "2026-11-14T00:00:00Z", time_uncertainty_s: 86400 });
  result.image_origin_id = null;
  const finding = memeFinding(result);
  assert.equal(finding.kind, "original");
  assert.doesNotMatch(finding.sentence, /later cop/);
  assert.match(finding.sentence, /1 copy is dated too close to the post/);
  const after = imageLookup({ time_source: "page", first_seen: "2026-11-16T00:00:00Z", time_uncertainty_s: 86400 });
  after.image_origin_id = null;
  assert.match(memeFinding(after).sentence, /1 later copy found/);
});
