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
