import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/data/shared.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { sharedRuns, highlightShared, sameTextKey } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`
);

const original = "URGENT : menace d'enlèvement en France, besoin de la commu crypto FR";

test("a copy with words added in front highlights only what it shares", () => {
  const copy = "France is becoming a hell hole. Nuke it already > URGENT : menace d'enlèvement en France, besoin de la commu";
  assert.deepEqual(sharedRuns(copy, original), ["URGENT : menace d'enlèvement en France, besoin de la commu"]);
});

test("case and surrounding punctuation don't break a run; the run is quoted as the copy wrote it", () => {
  assert.deepEqual(sharedRuns("so: “urgent menace d'enlèvement EN FRANCE!” they said", "URGENT : menace d'enlèvement en France"), ["“urgent menace d'enlèvement EN FRANCE!”"]);
  assert.deepEqual(sharedRuns("they said Menace d'enlèvement EN FRANCE! today", original), ["Menace d'enlèvement EN FRANCE!"]);
});

test("fewer than three words in a row, or an unrelated text, is not a match", () => {
  assert.deepEqual(sharedRuns("en France today", original), []);
  assert.deepEqual(sharedRuns("nothing in common here at all", original), []);
  assert.deepEqual(sharedRuns("", original), []);
});

test("every other post is marked against the submitted one; the submitted one is left alone", () => {
  const c = { submittedId: "a", originId: "b", nodes: [
    { id: "a", text: original },
    { id: "b", text: `seen this? ${original}`, textMatch: ["seen this?"] },
    { id: "c", text: "an unrelated reply" },
  ] };
  highlightShared(c);
  assert.equal(c.nodes[0].textMatch, undefined);
  assert.deepEqual(c.nodes[1].textMatch, ["seen this?", original]);
  assert.equal(c.nodes[2].textMatch, undefined);
});

test("only the same text, long enough to mean something, shares a stack key", () => {
  assert.equal(sameTextKey({ text: "Buy  now:\n50% off today" }), sameTextKey({ text: "Buy now: 50% off today" }));
  assert.notEqual(sameTextKey({ text: "Buy now: 50% off today" }), sameTextKey({ text: "Buy now: 60% off today" }));
  assert.equal(sameTextKey({ text: "ok" }), "");
  assert.equal(sameTextKey({ text: "Buy now: 50% off today", imageMatch: {} }), "");
});
