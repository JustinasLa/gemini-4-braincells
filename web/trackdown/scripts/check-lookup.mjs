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
  const formatMod = await server.ssrLoadModule("/src/ui/format.ts");
  const { isLineageEdge } = formatMod;
  const { clusterCase } = await server.ssrLoadModule("/src/data/api.ts");
  const { sharedTextHtml } = await server.ssrLoadModule("/src/ui/sharedText.ts");
  const { Panel } = await server.ssrLoadModule("/src/ui/panel.ts");
  const dump = process.argv.indexOf("--dump");
  if (dump > 0) writeFileSync(process.argv[dump + 1], JSON.stringify(mockLookupResult));

  const renderAll = (trace) => trace.nodes.map((node, i) => cardHtml(node, false, i + 1) + cardHtml(node, true, i + 1)).join("\n");
  const fixture = () => structuredClone(mockLookupResult);

  const textNode = (id, text, textMatch) => ({ id, text, textMatch, author: { name: id, handle: id }, role: "source", platform: "web", t: 0 });
  check("underlined phrases link to posts containing that phrase, preferring the submission", () => {
    const current = textNode("current", "Colette Kress was ‘a deer in the headlights,’ they couldn’t afford its chips.", ["Colette Kress was", "‘a deer in the headlights,’", "afford its chips."]);
    const other = textNode("other", 'Colette Kress was "a deer in the headlights," according to the report.');
    const submitted = textNode("submitted", "COLETTE KRESS WAS surprised. They could not afford its chips!");
    const html = sharedTextHtml(current, { nodes: [current, other, submitted], submittedId: submitted.id });
    assert.match(html, /data-go="submitted"[^>]*>Colette Kress was<\/a>/);
    assert.match(html, /data-go="other"[^>]*>‘a deer in the headlights,’<\/a>/);
    assert.match(html, /data-go="submitted"[^>]*>afford its chips\.<\/a>/);
    assert.doesNotMatch(html, /data-go="current"/);
  });

  check("unmatched highlights stay visible and matching text and attributes stay escaped", () => {
    const current = textNode("current", "<b>shared phrase here</b> & another unmatched phrase", ["<b>shared phrase here</b>", "another unmatched phrase"]);
    const target = textNode('target"<', current.text.split(" & ")[0]);
    const html = sharedTextHtml(current, { nodes: [current, target] });
    assert.match(html, /data-go="target&quot;&lt;"/);
    assert.match(html, /&lt;b&gt;shared phrase here&lt;\/b&gt;<\/a> &amp;/);
    assert.match(html, /<mark class="caption-hit">another unmatched phrase<\/mark>/);
    assert.doesNotMatch(html, /<b>|data-go="current"/);
    assert.equal(sharedTextHtml(current, { nodes: [current] }), formatMod.markRuns(current.text, current.textMatch));
  });

  check("clicking underlined text opens the matching post and focuses it on the canvas", () => {
    const listeners = new Map();
    const el = { addEventListener: (type, fn) => listeners.set(type, fn), querySelector: () => null, innerHTML: "" };
    const source = textNode("source", "Find another matching passage here.", ["another matching passage"]);
    const target = textNode("target", "Another matching passage appears elsewhere.");
    const focused = [];
    const g = { selId: source.id, k: 0.5, pathToOrigin: () => [], focus: (...args) => focused.push(args) };
    const panel = new Panel(el, g);
    // Exercise the detail renderer and real delegated click listener without mounting the meme UI.
    panel.render = () => { el.innerHTML = panel.detail(panel.c.nodes.find((n) => n.id === g.selId)); };
    g.select = (id) => { g.selId = id; panel.showFolio(true); };
    panel.tab = "folio";
    panel.set({ id: "test", nodes: [source, target], edges: [], receipts: [], submittedId: source.id }, false);
    assert.match(el.innerHTML, /class="caption-hit" href="#" data-go="target"/);
    let prevented = false;
    listeners.get("click")({ preventDefault: () => { prevented = true; }, target: { closest: () => ({ dataset: { go: target.id } }) } });
    assert.equal(prevented, true);
    assert.equal(g.selId, target.id);
    assert.equal(panel.tab, "folio");
    assert.match(el.innerHTML, /Another matching passage/);
    assert.deepEqual(focused, [[target.id, 0.95]]);
  });

  check("post details keep timestamp labels and receipts without the layout disclaimer", () => {
    const panel = new Panel({ addEventListener() {} }, {});
    for (const timeUnknown of [true, false]) {
      const node = { ...textNode("post", "A post with evidence"), timeUnknown,
        realData: [{ label: "Post data", facts: [{ label: "Text", value: "Evidence retained" }], receiptId: 42, url: "https://example.com/post" }] };
      panel.c = { nodes: [node], edges: [], receipts: [] };
      const html = panel.detail(node);
      assert.match(html, /Real data/);
      assert.match(html, /Evidence retained/);
      assert.match(html, /Receipt #42/);
      assert.match(html, /href="https:\/\/example.com\/post"/);
      assert.match(html, /Posted/);
      assert.equal(html.includes('class="v2">Time unknown</div>'), timeUnknown);
      assert.doesNotMatch(html, /Time unknown:|positioned at the submitted|publication timestamp/);
    }
  });

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
    // variant links are visual relationships, never copy lineage
    assert.ok(trace.edges.some((e) => e.rel === "variant"));
    assert.ok(!trace.edges.some((e) => e.rel === "variant" && isLineageEdge(e, trace.nodes.find((n) => n.id === e.from), trace.nodes.find((n) => n.id === e.to))));
    renderAll(lookupCase({ ...memeFixture("meme-blocked"), image_matches: [{ id: "img:bare" }], image_origin_id: "img:bare" }));
  });

  check("panel: a cluster's non-http source URLs never become links", () => {
    const bad = "javascript:alert(1)";
    const trace = clusterCase({ id: 8, status: "insufficient", score: 0.2, confidence: 0.2, synthetic: false, signals: [], analyst: null, members: [],
      posts: [{ uri: "at://did:plc:fictional2/app.bsky.feed.post/1", did: "did:plc:fictional2", text: "a post", created_at: "2026-11-13T10:00:00Z", embed_urls: [], media_hashes: [], url: bad }],
      evidence: [{ id: 2, kind: "source", summary: "a page", detail: {}, source_url: bad }] });
    // the private render methods need only the case and a selection, not a DOM
    const panel = Object.assign(Object.create(Panel.prototype), { c: trace, g: { selId: null } });
    const html = trace.nodes.map((n) => panel.detail(n)).join("");
    assert.ok(trace.nodes.some((n) => n.url === bad), "fixture carries the unsafe URL");
    assert.doesNotMatch(html, /href="javascript:/i);
  });

  check("a known live verdict with missing confidence does not claim there is no call", () => {
    const trace = clusterCase({ id: 8, status: "coordinated", score: 0.97, confidence: null,
      synthetic: false, members: [], posts: [], evidence: [] });
    const panel = Object.assign(Object.create(Panel.prototype), { c: trace, g: { selId: null } });
    const html = panel.verdict(trace);
    assert.equal(trace.status, "coordinated");
    assert.match(html, /Not recorded/);
    assert.doesNotMatch(html, /No call|doesn't settle/);
  });

  const { fmtDur, fmtNum } = await server.ssrLoadModule("/src/ui/format.ts");
  check("format: rounding never shows 60 s, 60 min or 1000k", () => {
    assert.equal(fmtDur(59_700), "1 min");
    assert.equal(fmtDur(59.7 * 60_000), "1 h");
    assert.equal(fmtNum(999_700), "1M");
    assert.equal(fmtNum(9_400), "9.4k");
    assert.equal(fmtNum(42_000), "42k");
  });

  check("an upload's text hits highlight the 3+ caption words they share", () => {
    const { markRuns } = formatMod;
    assert.equal(markRuns("a <b> & c"), "a &lt;b&gt; &amp; c");
    assert.equal(markRuns("so this is fine, this is fine", ["this is fine"]), 'so <mark class="caption-hit">this is fine</mark>, <mark class="caption-hit">this is fine</mark>');
    assert.equal(markRuns("when the comets land", ["when the comets", "the comets land"]), '<mark class="caption-hit">when the comets</mark><mark class="caption-hit"> land</mark>');
    const lookup = fixture();
    const post = lookup.posts.find((p) => p.role !== "submitted");
    post.text = "Look <here>: when the comets land, nobody tells you";
    post.caption_match = ["when the comets land", "nobody tells you"];
    const node = lookupCase(lookup).nodes.find((n) => n.id === post.id);
    const html = cardHtml(node, false, 1);
    assert.match(html, /Look &lt;here&gt;: <mark class="caption-hit">when the comets land<\/mark>, <mark class="caption-hit">nobody tells you<\/mark>/);
  });
} finally {
  await server.close();
}
if (failed) {
  console.log(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("all lookup rendering checks passed");
