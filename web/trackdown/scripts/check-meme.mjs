import assert from "node:assert/strict";
import { build } from "esbuild";

const bundle = await build({ stdin: { contents: 'export * from "./src/data/imageTrace.ts"; export * from "./src/data/lookup.ts"; export * from "./src/data/mockMeme.ts"; export { comparisonSource } from "./src/ui/compare.ts"; export { isLineageRel } from "./src/ui/format.ts"; export { hasVerdict } from "./src/data/lookup.ts";', resolveDir: process.cwd(), loader: "ts" }, bundle: true, write: false, platform: "node", format: "esm" });
const api = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
let checks = 0;
const check = (name, run) => { run(); checks++; console.log(`✓ ${name}`); };
const fixture = api.memeFixture();
check("image matches flow through lookupCase with original styling and variant edges", () => {
  const trace = api.lookupCase(fixture);
  assert.equal(trace.nodes.length, 7);
  assert.equal(trace.nodes.find((node) => node.id === "img:origin").role, "origin");
  assert.equal(trace.nodes.find((node) => node.id === "img:edited").role, "mutation");
  assert.deepEqual(trace.edges.map((edge) => `${edge.from}>${edge.to}:${edge.rel}`).sort(), ["img:origin>img:cropped:variant", "img:origin>img:edited:variant", `img:origin>${fixture.subject_id}:variant`].sort(),
    "edited/cropped matches and the submission link to the Original; unknown/same/resized get no link; no order is claimed for a crawl date");
  assert.equal(trace.originId, "", "a crawl-dated original is no trace origin: no age is derived from it");
  assert.ok(trace.nodes.find((node) => node.id === "img:origin").undated, "a crawl date is not shown as a publication time");
  const paged = structuredClone(fixture);
  paged.image_matches.find((match) => match.id === "img:origin").time_source = "page";
  assert.equal(api.lookupCase(paged).originId, "img:origin", "a page-dated original before the post is the origin");
  assert.ok(api.lookupCase(paged).edges.some((edge) => edge.from === "img:origin" && edge.to === fixture.subject_id && edge.rel === "media"), "and the submission is its same-image copy");
  const upload = { ...structuredClone(paged), kind: "image" };
  upload.posts[0].created_at = null;
  assert.equal(api.lookupCase(upload).originId, "", "an upload has no time of its own, so no age either");
  assert.ok(!trace.nodes.find((node) => node.id === "img:edited").undated, "a page date is");
  assert.ok(!trace.nodes.some((node) => node.id === "img:different"));
  assert.ok(!trace.steps.flatMap((step) => step.found).includes("img:different"));
  assert.ok(trace.steps[0].found.includes("img:edited"));
});
check("a post origin found by the lookup stays the origin; the image original is still linked", () => {
  const withPost = structuredClone(fixture);
  withPost.posts.push({ ...withPost.posts[0], id: "x:older", role: "origin", created_at: "2026-11-01T00:00:00Z", media_urls: [] });
  withPost.origin_id = "x:older";
  withPost.edges = [{ from: "x:older", to: fixture.subject_id, rel: "copy", tool: "oxylabs", note: null }];
  const trace = api.lookupCase(withPost);
  assert.equal(trace.originId, "x:older");
  const into = trace.edges.filter((edge) => edge.to === fixture.subject_id);
  assert.equal(into[0].rel, "copy", "post lineage keeps priority for the path to the origin");
  assert.ok(into.some((edge) => edge.rel === "variant" && edge.from === "img:origin"));
});
check("an original published after the submitted post is not its origin; a later crawl date says nothing", () => {
  const later = structuredClone(fixture);
  const origin = later.image_matches.find((match) => match.id === "img:origin");
  Object.assign(origin, { first_seen: "2026-11-20T00:00:00Z", time_source: "page" });
  assert.equal(api.imageTraceState(later).origin, undefined);
  assert.equal(api.lookupCase(later).originId, "");
  origin.time_source = "crawl_date";
  assert.equal(api.imageTraceState(later).origin?.id, "img:origin");
});
check("real server: image_matches [] and skipped steps on a text lookup show no meme trace", () => {
  const text = structuredClone(fixture);
  Object.assign(text, { kind: "text", image_matches: [], image_origin_id: null });
  text.steps = text.steps.map((step) => ({ ...step, state: "skipped", detail: "this input has no image to trace", found: [] }));
  assert.equal(api.imageTraceState(text).enabled, false);
  const image = { ...structuredClone(text), kind: "image" };
  image.steps = image.steps.map((step) => ({ ...step, detail: "reverse image search needs a public image URL; configure PUBLIC_HOST" }));
  const state = api.imageTraceState(image);
  assert.ok(state.enabled && !state.noEarlier, "a skipped search on an image is explained, not reported as no earlier copy");
  assert.match(state.skipped[0], /skipped · reverse image search needs a public image URL/);
});
check("fraction boxes belong to the upload; pixel boxes to the match", () => {
  assert.equal(api.boxFrame([0.1, 0.2, 0.9, 0.8]), "upload");
  assert.deepEqual(api.normalizedBox([0.1, 0.2, 0.9, 0.8], null, null).map((v) => Math.round(v)), [10, 20, 80, 60]);
  assert.equal(api.boxFrame([0, 0, 388, 40]), "match");
  assert.equal(api.normalizedBox([0, 0, 388, 40], null, null), null, "pixel boxes need the match size");
  assert.equal(api.normalizedBox([0.5, 0.5, 0.2, 0.9]), null, "reversed fractions");
});
check("engine receipts are attributed to the Oxylabs scrape, downloads to fetch", () => {
  const withReceipts = structuredClone(fixture);
  withReceipts.receipts.push({ ...withReceipts.receipts[0], id: 13, tool: "tineye" }, { ...withReceipts.receipts[0], id: 14, tool: "fetch" });
  const tools = api.lookupCase(withReceipts).receipts.map((receipt) => receipt.tool);
  assert.ok(tools.includes("oxylabs") && tools.includes("fetch") && !tools.includes("archive"));
});
check("an upload with only crawl-dated matches states no origin and no ages", () => {
  const trace = api.lookupCase(api.memeFixture("meme-upload"));
  assert.equal(trace.originId, "");
  assert.ok(trace.nodes.find((node) => node.id === "x:submitted").timeUnknown);
  assert.ok(trace.nodes.filter((node) => node.imageMatch).every((node) => node.undated));
  assert.equal(api.imageTraceState(api.memeFixture("meme-upload")).origin?.id, "img:origin", "the Original card still shows");
});
check("server variant edges win and keep their tool; variant links are not copy lineage", () => {
  const served = structuredClone(fixture);
  served.edges = [{ from: "img:same", to: "img:edited", rel: "variant", sim: null, tool: "nemotron", note: "Caption changed" }];
  const edges = api.lookupCase(served).edges.filter((edge) => edge.rel === "variant");
  assert.ok(edges.some((edge) => edge.from === "img:same" && edge.to === "img:edited" && edge.tool === "nemotron" && edge.note === "Caption changed"));
  assert.ok(!edges.some((edge) => edge.to === "img:cropped"), "no fallback edges once the server sent its own");
  assert.equal(api.isLineageRel("variant"), false);
});
check("an image origin already on the map (same id as a post) still anchors the links", () => {
  const clash = structuredClone(fixture);
  clash.posts.push({ ...clash.posts[0], id: "img:origin", role: "copy", created_at: "2026-11-01T00:00:00Z" });
  const trace = api.lookupCase(clash);
  assert.ok(trace.edges.some((edge) => edge.from === "img:origin" && edge.to === "img:edited"));
});
check("change sentences say which way to read them (changes_from), older servers stay neutral", () => {
  assert.equal(api.changeDirection({ changes_from: "match" }).heading, "This copy → your picture");
  assert.equal(api.changeDirection({ changes_from: "upload" }).heading, "Your picture → this copy");
  assert.equal(api.changeDirection({}).heading, "");
  assert.equal(api.changeDirection({ changes_from: "sideways" }).heading, "", "an unknown value reads as unstated");
  const upload = api.memeFixture("meme-upload");
  upload.image_matches = upload.image_matches.filter((match) => match.relation === "edited");
  upload.image_origin_id = null;
  upload.image_matches[0].changes_from = "upload";
  const finding = api.memeFinding(upload);
  assert.equal(finding?.kind, "edited");
  assert.match(finding.sentence, /From this picture to that copy, as a model reads it:/);
  delete upload.image_matches[0].changes_from;
  assert.match(api.memeFinding(upload).sentence, /What changed, as a model reads it:/, "older servers keep the neutral lead-in");
});
check("relation groups count matches, exclude original and different", () => {
  const state = api.imageTraceState(fixture);
  assert.deepEqual(state.groups.map((group) => [group.relation, group.matches.length]), [["same", 1], ["resized", 1], ["cropped", 1], ["edited", 1], ["unknown", 1]]);
  assert.equal(state.different.length, 1);
});
check("empty result and done reverse step without matches never invent an origin", () => {
  const empty = api.memeFixture("meme-empty");
  assert.ok(api.imageTraceState(empty).noEarlier);
  assert.equal(api.lookupCase(empty).nodes.length, 1);
  delete empty.image_matches;
  assert.ok(api.imageTraceState(empty).noEarlier);
});
check("legacy server is unchanged and has no image panel", () => {
  const legacy = structuredClone(fixture);
  delete legacy.image_matches; delete legacy.image_origin_id; legacy.steps = [];
  const trace = api.lookupCase(legacy);
  assert.equal(api.imageTraceState(legacy).enabled, false);
  assert.equal(api.appendImageTrace(trace), trace);
});
check("failed steps keep details and the submitted trace", () => {
  const failed = api.memeFixture("meme-failed");
  failed.steps[0].state = "failed"; failed.steps[0].detail = "Service unavailable";
  assert.match(api.imageTraceState(failed).failures[0], /Service unavailable/);
  assert.equal(api.imageTraceState(failed).noEarlier, false);
  assert.equal(api.lookupCase(failed).nodes.length, 1);
});
check("loading steps are visible without declaring an empty result", () => {
  const loading = api.memeFixture("meme-empty");
  loading.steps[0].state = "running"; loading.steps[1].state = "pending";
  assert.ok(api.imageTraceState(loading).searching);
  assert.ok(api.imageTraceState(loading).comparing);
  assert.equal(api.imageTraceState(loading).noEarlier, false);
});
check("without an original, variations connect to the submission", () => {
  const noOrigin = { ...fixture, image_origin_id: null };
  assert.ok(api.imageTraceState(noOrigin).noEarlier);
  assert.ok(api.lookupCase(noOrigin).edges.every((edge) => edge.from === fixture.subject_id));
  assert.ok(!api.lookupCase(noOrigin).nodes.some((node) => node.role === "origin"));
});
check("submission cannot become its own image origin and duplicate IDs are ignored", () => {
  const duplicate = { ...fixture, image_origin_id: fixture.subject_id, image_matches: [...fixture.image_matches, { id: fixture.subject_id }, fixture.image_matches[0], {}] };
  assert.equal(api.imageTraceState(duplicate).origin, undefined);
  const trace = api.lookupCase(duplicate);
  assert.equal(new Set(trace.nodes.map((node) => node.id)).size, trace.nodes.length);
  assert.ok(trace.edges.every((edge) => edge.from !== edge.to));
});
check("unknown fields remain neutral and undated matches use layout-only time", () => {
  const match = { id: "img:new", relation: "new-relation", engine: "new-engine", time_source: "new-clock", first_seen: "2026-01-01", change_kinds: ["new_kind"] };
  assert.equal(api.relationOf(match), "unknown");
  assert.equal(api.engineLabel(match), "Search engine unknown");
  assert.equal(api.firstSeenLabel(match), "date unknown");
  assert.equal(api.humanLabel("new_kind"), "New kind");
  const trace = api.lookupCase({ ...fixture, image_matches: [match], image_origin_id: null });
  assert.ok(trace.nodes.find((node) => node.id === match.id).timeUnknown);
});
check("pixel boxes scale, clamp, and reject malformed or reversed coordinates", () => {
  assert.deepEqual(api.normalizedBox([-10, 20, 120, 80], 100, 100), [0, 20, 100, 60]);
  for (const box of [null, [], [0, 0, 10], [10, 0, 0, 10], [0, 0, NaN, 10], [0, 0, Infinity, 10], ["0", 0, 10, 10]]) assert.equal(api.normalizedBox(box, 100, 100), null);
  assert.equal(api.normalizedBox([0, 0, 10, 10], 0, 100), null);
  assert.equal(api.normalizedBox([0, 0, 10, 10], 100, undefined), null);
});
check("crawl dates are upper bounds, page and post dates are labelled distinctly", () => {
  assert.match(api.firstSeenLabel(fixture.image_matches[0]), /^first seen by TinEye on 2026-11-12 · upper bound, not a publish date$/);
  assert.equal(api.firstSeenLabel({ first_seen: "2012-03-01", time_source: "page" }), "page dated 2012-03-01");
  assert.match(api.firstSeenLabel({ first_seen: "2012-03-01", time_source: "snowflake" }), /^time from post id/);
  assert.equal(api.firstSeenLabel({ first_seen: "invalid", time_source: "page" }), "date unknown");
});
check("unsafe source URLs are not links or images", () => {
  assert.equal(api.pageUrl("javascript:alert(1)"), undefined);
  assert.equal(api.imageUrl("javascript:alert(1)"), undefined);
  assert.equal(api.imageUrl("//bad.example/image"), undefined);
  assert.equal(api.pageUrl("data:text/html,test"), undefined);
});
check("comparison uses the upload endpoint or the submitted post's first media", () => {
  const link = api.lookupCase(fixture);
  assert.equal(api.comparisonSource(link), fixture.posts[0].media_urls[0]);
  const upload = api.lookupCase({ ...fixture, kind: "image", id: "upload/a" });
  assert.equal(api.comparisonSource(upload), "/api/lookup/upload%2Fa/image");
  assert.equal(api.comparisonSource(api.lookupCase({ ...fixture, posts: [] })), undefined);
});
check("conversion does not mutate the server result and minimal optional matches are safe", () => {
  const snapshot = structuredClone(fixture);
  api.lookupCase(fixture);
  assert.deepEqual(fixture, snapshot);
  const trace = api.lookupCase({ ...fixture, image_matches: [{ id: "img:minimal" }], image_origin_id: null });
  assert.equal(trace.nodes.at(-1).imageMatch.id, "img:minimal");
  assert.equal(trace.nodes.at(-1).timeUnknown, true);
});
check("finding: an X post whose same picture was online earlier is a Copy; edits elsewhere are counted", () => {
  const f = api.memeFinding(fixture);
  assert.equal(f.kind, "copy");
  assert.equal(f.match.id, "img:origin", "the earliest older match decides (crawl date + one day still before the post)");
  assert.match(f.sentence, /archive\.example before this post/);
  assert.match(f.sentence, /1 edited version of it was also found/);
});
check("finding: when the earliest older match is edited, the submission is Edited, with the model's change list", () => {
  const edited = structuredClone(fixture);
  edited.image_matches.find((match) => match.id === "img:edited").first_seen = "2026-11-01T00:00:00Z";
  const f = api.memeFinding(edited);
  assert.equal(f.kind, "edited");
  assert.match(f.sentence, /From that copy to this picture, as a model reads it: Caption changed/);
});
check("finding: nothing dated before the post is Original; undated copies are named as possibly older", () => {
  const later = structuredClone(fixture);
  for (const match of later.image_matches) if (match.first_seen) Object.assign(match, { first_seen: "2026-11-20T00:00:00Z", time_source: "page" });
  later.image_origin_id = null;
  const f = api.memeFinding(later);
  assert.equal(f.kind, "original");
  assert.match(f.sentence, /2 undated copies could still be older/);
  const crawl = structuredClone(fixture);
  for (const match of crawl.image_matches) if (match.first_seen) Object.assign(match, { first_seen: "2026-11-13T20:00:00Z", time_source: "crawl_date" });
  assert.equal(api.memeFinding(crawl).kind, "original", "a crawl date within a day of the post is not provably older");
});
check("finding: no related match is First sighting; 'different' matches don't count", () => {
  const none = { ...structuredClone(fixture), image_matches: fixture.image_matches.filter((match) => match.relation === "different"), image_origin_id: null };
  assert.equal(api.memeFinding(none).kind, "first");
  assert.equal(api.memeFinding(api.memeFixture("meme-empty")).kind, "first");
});
check("finding: an upload's matches were all online before it, dated or not", () => {
  const upload = api.memeFixture("meme-upload");
  const f = api.memeFinding(upload);
  assert.equal(f.kind, "copy");
  assert.match(f.sentence, /before this upload/);
  const undated = structuredClone(upload);
  for (const match of undated.image_matches) Object.assign(match, { first_seen: null, time_source: null });
  assert.equal(api.memeFinding(undated).kind, "copy", "a same-picture match outranks an edited one when none is dated");
});
check("finding: pending while searching or comparing; none for failed, skipped or text lookups", () => {
  const running = structuredClone(fixture);
  running.steps[1].state = "running";
  assert.equal(api.memeFinding(running), "pending");
  const failed = api.memeFixture("meme-failed");
  failed.steps[0].state = "failed";
  assert.equal(api.memeFinding(failed), null);
  const text = { ...structuredClone(fixture), kind: "text", image_matches: [], image_origin_id: null };
  text.steps = text.steps.map((step) => ({ ...step, state: "skipped" }));
  assert.equal(api.memeFinding(text), null);
  const untimed = structuredClone(fixture);
  untimed.posts[0].created_at = null;
  assert.equal(api.memeFinding(untimed).kind, "seen", "a post with no time can't be ordered against its copies");
  const unknownOnly = { ...structuredClone(fixture), image_matches: fixture.image_matches.filter((match) => match.id === "img:unknown").map((match) => ({ ...match, first_seen: "2026-11-01T00:00:00Z", time_source: "page" })) };
  assert.equal(api.memeFinding(unknownOnly).kind, "seen", "an older match whose relation wasn't checked is no Copy claim");
});
check("coin only for a real verdict: a lookup's insufficient is no call, every other case keeps its verdict", () => {
  assert.equal(api.hasVerdict(api.lookupCase(fixture)), false);
  assert.equal(api.hasVerdict({ ...api.lookupCase(fixture), status: "coordinated" }), true);
  assert.equal(api.hasVerdict({ status: "insufficient" }), true, "a simulated or monitored case's Can't tell is a detector result");
});
console.log(`\n${checks} meme trace checks passed.`);
