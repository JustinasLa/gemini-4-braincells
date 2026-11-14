// Rendering regression for live-lookup data: converts the development fixture (and variants of
// it) with lookupCase() and renders every card, failing on a throw or a broken expectation.
// No test framework: Vite (already a dev dependency) loads the TypeScript sources directly.
//   node scripts/check-lookup.mjs                 run the checks
//   node scripts/check-lookup.mjs --dump out.json also write the fixture as a lookup API response
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const server = await createServer({ root, configFile: false, logLevel: "error", appType: "custom", server: { middlewareMode: true, hmr: false } });
let failed = 0;
const check = (name, fn) => {
  try { fn(); console.log(`ok   ${name}`); }
  catch (error) { failed++; console.log(`FAIL ${name}\n     ${error?.stack ?? error}`); }
};
try {
  const { lookupCase } = await server.ssrLoadModule("/src/data/lookup.ts");
  const { mockLookupResult } = await server.ssrLoadModule("/src/data/mockLookup.ts");
  const { cardHtml } = await server.ssrLoadModule("/src/ui/card.ts");
  const { isLineageEdge } = await server.ssrLoadModule("/src/ui/format.ts");
  const { clusterCase } = await server.ssrLoadModule("/src/data/api.ts");
  const dump = process.argv.indexOf("--dump");
  if (dump > 0) writeFileSync(process.argv[dump + 1], JSON.stringify(mockLookupResult));

  const renderAll = (trace) => trace.nodes.map((node, i) => cardHtml(node, false, i + 1) + cardHtml(node, true, i + 1)).join("\n");
  const fixture = () => structuredClone(mockLookupResult);

  check("fixture has a screenshotted post, a screenshot_of edge and screenshot receipts", () => {
    const r = fixture();
    assert.ok(r.posts.some((p) => p.role === "screenshotted"));
    assert.ok(r.edges.some((e) => e.rel === "screenshot_of"));
    assert.ok((r.screenshots ?? []).length >= 2);
  });

  check("lookupCase + cardHtml render the screenshot fixture", () => {
    const trace = lookupCase(fixture());
    const html = renderAll(trace);
    const shot = trace.nodes.find((n) => n.id === "x:98");
    assert.equal(shot.role, "screenshotted");
    assert.match(cardHtml(shot, false, 1), /Real post in the screenshot/);
    assert.ok(!/fake/i.test(html), "no card says 'fake'");
  });

  check("screenshotted post stays out of the copy sample, origin and copy links", () => {
    const trace = lookupCase(fixture());
    const copies = trace.clusters.find((c) => c.id === "lookup-copies");
    assert.ok(copies && !copies.members.includes("x:98"));
    assert.notEqual(trace.originId, "x:98");
    const edge = trace.edges.find((e) => e.rel === "screenshot_of");
    assert.deepEqual([edge.from, edge.to, edge.note], ["x:100", "x:98", "Differs from the real post"]);
    assert.equal(trace.nodes.find((n) => n.id === "x:98").cluster, undefined);
  });

  check("cluster view: a worker screenshot_receipt row shows on the posts carrying it", () => {
    const uri = "at://did:plc:fictional1/app.bsky.feed.post/3kfict";
    const receipt = { media_hash: "bafyfict", label: "differs", reason: "The screenshot differs from @ada.example.com's real post on Bluesky: 1 word changed.", platform: "bluesky", handle: "ada.example.com", shown_text: "A <b>fictional</b> claim", original_text: "A fictional claim, edited", original_url: "https://bsky.app/profile/ada.example.com/post/3kreal", diff: [{ op: "same", text: "A" }, { op: "shot_only", text: "<b>fictional</b>" }], differences: [], shown_counts: {}, ui_oddities: [], receipt_ids: [], time_check: "unknown" };
    const d = { id: 7, status: "insufficient", score: 0.4, confidence: 0.3, synthetic: false, signals: [], analyst: null, members: [],
      posts: [{ uri, did: "did:plc:fictional1", text: "look at this", created_at: "2026-11-13T10:00:00Z", embed_urls: [], media_hashes: ["bafyfict"] }],
      evidence: [{ id: 1, kind: "media", summary: "Screenshot of @ada.example.com's post on Bluesky: differs from the real post.", detail: { type: "screenshot_receipt", media_hash: "screenshot-receipt:bafyfict", post_uris: [uri], receipt } }] };
    const trace = clusterCase(d);
    const node = trace.nodes.find((n) => n.id === uri);
    assert.ok(node.realData.some((g) => g.label === "Screenshot check"), "group attached");
    assert.ok(node.flags.includes("screenshot differs from the real post"));
    const html = renderAll(trace);
    assert.ok(!html.includes("<b>fictional</b>"), "escaped");
  });

  check("a reply to a screenshotted post is not lineage (no parent, no earlier post)", () => {
    const shotted = { role: "screenshotted" }, submitted = { role: "submitted" }, copy = { role: "relay" };
    assert.equal(isLineageEdge({ rel: "reply" }, shotted, submitted), false);
    assert.equal(isLineageEdge({ rel: "copy" }, submitted, shotted), false);
    assert.equal(isLineageEdge({ rel: "copy" }, submitted, copy), true);
    assert.equal(isLineageEdge({ rel: "screenshot_of" }, submitted, copy), false);
  });

  check("screenshot check is on the real post and the post carrying the screenshot", () => {
    const trace = lookupCase(fixture());
    const groups = (id) => trace.nodes.find((n) => n.id === id).realData.filter((g) => g.label === "Screenshot check");
    const real = groups("x:98");
    assert.equal(trace.nodes.find((n) => n.id === "x:98").realData[0].label, "Screenshot check", "check comes first on the real post");
    assert.equal(real.length, 1);
    const fact = (g, label) => g.facts.find((f) => f.label === label)?.value;
    assert.equal(fact(real[0], "Result"), "Differs from the real post");
    assert.match(fact(real[0], "Time check"), /unknown time zone/);
    assert.match(fact(real[0], "Differences"), /tonight/);
    assert.match(fact(real[0], "Counts"), /^Counts are context only/);
    const carrier = groups("x:100");
    assert.equal(carrier.length, 2);
    assert.equal(fact(carrier[1], "Result"), "No matching post found: it may be deleted, private or never posted");
    const flags = trace.nodes.find((n) => n.id === "x:100").flags;
    assert.ok(flags.includes("screenshot differs from the real post") && flags.includes("screenshot: no matching post found"));
    assert.ok(!/fake/i.test(JSON.stringify(trace.nodes.map((n) => n.realData))));
  });

  check("unchecked receipt says why", () => {
    const r = fixture();
    r.screenshots[1] = { ...r.screenshots[1], label: "unchecked", reason: "lookup budget used up" };
    const group = lookupCase(r).nodes.find((n) => n.id === "x:100").realData.filter((g) => g.label === "Screenshot check")[1];
    assert.equal(group.facts.find((f) => f.label === "Result").value, "Not checked: lookup budget used up");
  });

  check("unknown role and rel from a newer API do not crash", () => {
    const r = fixture();
    r.posts[3].role = "from_a_future_api";
    r.edges.push({ from: "x:100", to: "x:102", rel: "teleported", sim: 0.5, tool: "nemotron", note: null });
    r.screenshots.push({ ...r.screenshots[0], label: "some_new_label", original_id: "x:missing" });
    const trace = lookupCase(r);
    renderAll(trace);
    assert.equal(trace.nodes.find((n) => n.id === "x:102").role, "relay");
  });

  check("older API without screenshots, and a running job with nothing yet, still render", () => {
    const r = fixture();
    delete r.screenshots;
    r.edges = r.edges.filter((e) => e.rel !== "screenshot_of");
    renderAll(lookupCase(r));
    renderAll(lookupCase({ ...fixture(), state: "running", posts: [], edges: [], screenshots: [], pages: [], media: [] }));
  });

  const { memeFixture } = await server.ssrLoadModule("/src/data/mockMeme.ts");
  check("meme trace: image matches render as cards; an image with no text-search hits stays readable", () => {
    const trace = lookupCase(memeFixture());
    const html = renderAll(trace);
    assert.match(html, /ORIGINAL · EARLIEST MATCH FOUND/);
    assert.match(html, /first seen by TinEye on 2026-11-12 · upper bound, not a publish date/);
    assert.doesNotMatch(html, /\b(fake|deepfake)\b/i);
    assert.ok(trace.edges.some((e) => e.rel === "variant" && isLineageEdge(e, trace.nodes.find((n) => n.id === e.from), trace.nodes.find((n) => n.id === e.to))));
    renderAll(lookupCase({ ...memeFixture("meme-blocked"), image_matches: [{ id: "img:bare" }], image_origin_id: "img:bare" }));
  });
} finally {
  await server.close();
}
if (failed) {
  console.log(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("all lookup rendering checks passed");
