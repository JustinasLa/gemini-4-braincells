import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/engine/graph.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
  loader: { ".webp": "empty", ".css": "empty" },
});
const { Graph } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);

// Graph's constructor needs a DOM; the camera maths only needs these fields.
const camera = (k) => Object.assign(Object.create(Graph.prototype), { k, W: 800, H: 600, cx: 0, cy: 0, camAnim: null, zoomTarget: null, followUntil: 0 });

test("zooming out from below the manual limit (after fitting a long timeline) never zooms in", () => {
  const g = camera(0.03);
  g.zoomBy(1 / 1.35);
  assert.ok(g.zoomTarget.k <= 0.03, `target ${g.zoomTarget.k} jumped past the fitted 0.03`);
  g.zoomBy(1.35);
  assert.ok(g.zoomTarget.k > 0.03);
});

test("zoom stays inside its limits above the floor", () => {
  const g = camera(0.07);
  g.zoomBy(0.5);
  assert.equal(g.zoomTarget.k, 0.06);
  g.zoomBy(100);
  assert.equal(g.zoomTarget.k, 2.6);
});
