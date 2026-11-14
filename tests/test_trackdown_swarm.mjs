import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../web/trackdown/node_modules/esbuild/lib/main.js";

const { outputFiles } = await build({
  entryPoints: [fileURLToPath(new URL("../web/trackdown/src/data/swarm.ts", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const { layoutTimeline, swarmChips, swarmAria, axisLabels, durationWords, durationShort, firstWords, VERDICT, FAMILY_NAME } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`
);

const sig = (family, score, summary, polarity = "for") => ({ family, name: family, score, polarity, summary, numbers: {}, examples: [] });
const point = (t, extra = {}) => ({ t, a: 0, k: "post", g: 0, burst: false, ...extra });

test("verdict words and family names follow the product's words", () => {
  assert.equal(VERDICT.swarm.word, "Shill");
  assert.equal(VERDICT.organic.word, "Chill");
  assert.equal(VERDICT.unclear.word, "Can't tell");
  assert.deepEqual(FAMILY_NAME, { timing: "Timing", accounts: "Accounts", copy: "Copy-paste", boost: "Boosting" });
});

test("Shill chips: the strongest 'for' signal of each fired family, strongest first, at most four", () => {
  const chips = swarmChips({
    verdict: "swarm",
    fired: ["timing", "copy", "accounts", "boost"],
    signals: [
      sig("timing", 0.6, "posts 6 s apart"),
      sig("copy", 0.86, "same words x12"),
      sig("timing", 0.93, "31 accounts in 4 min"),
      sig("accounts", 0.72, "24 accounts made this week"),
      sig("boost", 0.5, "52 boosts in 5 min"),
      sig("accounts", 0.9, "old accounts", "against"),
    ],
  });
  assert.deepEqual(chips.map((c) => c.summary), ["31 accounts in 4 min", "same words x12", "24 accounts made this week", "52 boosts in 5 min"]);
  assert.deepEqual(chips.map((c) => c.label), ["Timing", "Copy-paste", "Accounts", "Boosting"]);
  assert.equal(swarmChips({ verdict: "swarm", fired: [], signals: [sig("timing", 0.9, "a"), sig("copy", 0.8, "b"), sig("boost", 0.7, "c"), sig("accounts", 0.6, "d")] }, 2).length, 2);
});

test("Shill chips skip families that did not fire, and empty summaries", () => {
  const chips = swarmChips({ verdict: "swarm", fired: ["copy"], signals: [sig("timing", 0.99, "fast"), sig("copy", 0.4, "same words x3"), sig("copy", 0.9, "  ")] });
  assert.deepEqual(chips.map((c) => c.summary), ["same words x3"]);
});

test("Chill chips are the strongest 'against' signals; Can't tell has no chips", () => {
  const signals = [sig("timing", 0.3, "6 accounts in 25 min"), sig("copy", 0.76, "64 different captions", "against"), sig("timing", 0.81, "spread over 18 h", "against")];
  assert.deepEqual(swarmChips({ verdict: "organic", fired: [], signals }).map((c) => c.summary), ["spread over 18 h", "64 different captions"]);
  // a guard's family would read wrong over it ("Copy-paste" over "64 different captions")
  assert.ok(swarmChips({ verdict: "organic", fired: [], signals }).every((c) => c.label === ""));
  assert.deepEqual(swarmChips({ verdict: "unclear", fired: [], signals }), []);
});

test("posts seconds apart stay one block in the middle, not stretched across the axis", () => {
  // real data: 5 accounts posted the same picture within 2 s
  const tight = layoutTimeline([0, 0.4, 1.3, 1.5, 2.1].map((t) => point(1e9 + t)), { cols: 48, rows: 10 });
  const cols = new Set(tight.dots.map((d) => d.col));
  assert.equal(cols.size, 1, `5 posts in 2 s share a column, got ${[...cols]}`);
  assert.ok(Math.abs([...cols][0] - 23.5) <= 1, "and sit in the middle");
  // 7 posts about 20 s apart: a narrow block, still centred
  const short = layoutTimeline(Array.from({ length: 7 }, (_, i) => point(i * 20)), { cols: 48, rows: 10 });
  const used = short.dots.map((d) => d.col);
  assert.ok(Math.max(...used) - Math.min(...used) <= 6, `7 posts in 2 min take a few columns, got ${Math.max(...used) - Math.min(...used) + 1}`);
  assert.ok(Math.abs((Math.max(...used) + Math.min(...used)) / 2 - 23.5) <= 1);
  // posts hours apart still use the whole width
  const spread = layoutTimeline(Array.from({ length: 5 }, (_, i) => point(i * 4 * 3600)), { cols: 48, rows: 10 });
  assert.equal(spread.dots[0].col, 0);
  assert.equal(spread.dots.at(-1).col, 47);
});

test("a burst becomes a solid block; scattered posts stay apart", () => {
  const points = [];
  for (let i = 0; i < 6; i++) points.push(point(i * 3600)); // one an hour
  for (let i = 0; i < 40; i++) points.push(point(6 * 3600 + i * 5, { burst: true })); // 40 in 200 s
  for (let i = 0; i < 6; i++) points.push(point(7 * 3600 + i * 3600));
  const { dots, cols, rows, burst } = layoutTimeline(points, { cols: 48, rows: 10 });
  assert.equal(dots.length, points.length);
  assert.ok(dots.every((d) => d.col >= 0 && d.col < cols && d.row >= 0 && d.row < rows));
  const burstCols = new Set(dots.filter((d) => d.burst).map((d) => d.col));
  assert.ok(burstCols.size >= 4 && burstCols.size <= 5, `40 burst dots in 10-high columns fill 4-5 columns, got ${burstCols.size}`);
  assert.equal(Math.max(...burstCols) - Math.min(...burstCols) + 1, burstCols.size, "the burst is one block");
  assert.deepEqual(burst, [Math.min(...burstCols), Math.max(...burstCols)]);
  const hourly = dots.filter((d) => !d.burst);
  assert.ok(hourly.every((d) => d.row === 0), "one post an hour never stacks");
  assert.equal(new Set(hourly.map((d) => d.col)).size, hourly.length);
});

test("dots keep time order, never exceed a column, and fit when everything is at the end", () => {
  const points = [point(0), ...Array.from({ length: 200 }, (_, i) => point(86400 + i))];
  const { dots, cols, rows } = layoutTimeline(points, { cols: 30, rows: 10 });
  const perCol = new Map();
  for (const d of dots) perCol.set(d.col, (perCol.get(d.col) ?? 0) + 1);
  assert.ok([...perCol.values()].every((n) => n <= rows));
  assert.ok(dots.every((d) => d.col < cols));
  for (let i = 1; i < dots.length; i++) assert.ok(dots[i].col >= dots[i - 1].col);
});

test("more sightings than the grid holds make the columns taller, not wider", () => {
  const points = Array.from({ length: 300 }, (_, i) => point(i));
  const { dots, rows, cols } = layoutTimeline(points, { cols: 20, rows: 10 });
  assert.equal(rows, 15);
  assert.ok(dots.every((d) => d.col < cols && d.row < rows));
});

test("one instant is centred; empty input is empty", () => {
  const { dots } = layoutTimeline(Array.from({ length: 5 }, () => point(100)), { cols: 21, rows: 10 });
  assert.ok(dots.every((d) => d.col === 10));
  assert.deepEqual(layoutTimeline([]).dots, []);
});

test("same-words groups are ranked by size and boosts are marked", () => {
  const points = [point(0, { g: 7 }), point(1, { g: 3 }), point(2, { g: 3 }), point(3, { g: 3 }), point(4, { k: "repost" }), point(5, { k: "like", g: 0 })];
  const { dots } = layoutTimeline(points);
  assert.deepEqual(dots.map((d) => d.group), [1, 0, 0, 0, -1, -1]);
  assert.deepEqual(dots.map((d) => d.boost), [false, false, false, false, true, true]);
});

test("the plot's label says it in a few words", () => {
  const start = "2026-11-14T10:00:00Z";
  assert.equal(swarmAria({ verdict: "swarm", n_accounts: 43, burst: { start, end: "2026-11-14T10:04:00Z", n_posts: 40, n_accounts: 31 }, first_at: null, last_at: null, timeline: [] }), "Shill: 31 accounts in 4 minutes");
  assert.equal(swarmAria({ verdict: "organic", n_accounts: 60, burst: null, first_at: start, last_at: "2026-11-15T04:00:00Z", timeline: [] }), "Chill: 60 accounts over 18 hours");
  assert.equal(swarmAria({ verdict: "unclear", n_accounts: 1, burst: null, first_at: null, last_at: null, timeline: [] }), "Can't tell: 1 account");
});

test("durations, axis labels and first words stay short", () => {
  assert.equal(durationWords(240), "4 minutes");
  assert.equal(durationWords(30), "30 seconds");
  assert.equal(durationWords(3 * 86400), "3 days");
  assert.equal(durationShort(240), "4 min");
  assert.equal(durationShort(18 * 3600), "18 h");
  assert.equal(durationShort(5400), "1.5 h");
  const first = Date.UTC(2026, 10, 14, 10, 0) / 1000;
  const [from, to] = axisLabels(first, first + 1740, first + 60);
  assert.match(from, /^\d\d:\d\d$/);
  assert.equal(to, "+29 min");
  assert.match(axisLabels(first, first + 86400, first + 3 * 86400)[0], /^\d{1,2} [A-Z][a-z]{2} \d\d:\d\d$/);
  assert.equal(axisLabels(first, first, first)[1], "");
  assert.equal(firstWords("  one two\nthree four five six seven eight nine ten "), "one two three four five six seven eight…");
});
