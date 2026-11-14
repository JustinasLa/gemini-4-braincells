import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/ui/panelwidth.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { PANEL_MIN, panelMax, clampPanel, stepPanel, parseSaved } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`
);

test("the panel stays between its minimum and what leaves the canvas room", () => {
  assert.equal(clampPanel(100, 1440), PANEL_MIN);
  assert.equal(clampPanel(700.4, 1440), 700);
  assert.equal(clampPanel(2000, 1440), 960);
  assert.equal(clampPanel(900, 1000), 620);
  assert.equal(panelMax(500), PANEL_MIN); // never below the minimum, even in a tiny window
});

test("arrow keys step from the chosen width; Home and End jump to the ends", () => {
  assert.equal(stepPanel(400, "ArrowLeft", false, 1440), 420);
  assert.equal(stepPanel(400, "ArrowRight", true, 1440), 320);
  assert.equal(stepPanel(950, "ArrowLeft", true, 1440), 960);
  assert.equal(stepPanel(400, "Home", false, 1440), 960);
  assert.equal(stepPanel(400, "End", false, 1440), PANEL_MIN);
  assert.equal(stepPanel(400, " ", false, 1440), null);
});

test("a saved width is read back only when it is a positive number", () => {
  assert.equal(parseSaved("701"), 701);
  assert.equal(parseSaved(null), null);
  assert.equal(parseSaved("abc"), null);
  assert.equal(parseSaved("-5"), null);
});
