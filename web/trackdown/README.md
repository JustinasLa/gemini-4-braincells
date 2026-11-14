# Shill or Chill · Trackdown

Trace one post back to the earliest copy we can find, then see how it spread and whether that
spread was coordinated. Each post is a card on an infinite canvas. Edges show how a card relates
to its parent: reposted, quoted, copied text, same image, shared link, cites.

```bash
cd web/trackdown
npm install
npm run dev                         # http://127.0.0.1:3001 (proxies /api to API_ORIGIN, default :8000)
API_ORIGIN=http://127.0.0.1:8020 npm run dev
npm run build                       # static files in dist/ (relative paths, works under any prefix)
npm run ship                        # build and copy to ../public/trackdown/ for Caddy
```

URL flags: `?case=tap-water|rhinoceros|bridge|flood|glow` picks an archive case, `?static` skips the intro run.
`?lookup=<URL-encoded link>` starts a live lookup. In the Vite development server only,
`?mockLookup` runs a progressive fictional fixture; the header, panel and console label it MOCK.
The fixture is excluded from production builds and never runs by default.

## Look

One visual language, taken from Junction 2026's banner: a Renaissance painting read by machine vision.

- The canvas sits on Botticelli's *Birth of Venus*, darkened, with its real ORB detections baked in
  (green boxes, match scores, dashed match lines), drifting slightly as you pan.
- Real computer vision, not decoration: the keypoints and descriptor-match scores come from OpenCV
  (`blender/venus_cv.py` -> `src/assets/venus-cv.json`). They animate in the splash, the trace window,
  the verdict banner and the verdict reveal. On cards, a green box shows a score only when the link
  that brought the post in has a measured similarity; otherwise it says TEXT or OBJ.
- Avatars are detected patches of the painting. Days-old accounts get an empty hatched box; templated
  handles (glow_fan_0412…) share one patch, so a wave of look-alikes looks alike.
- A cluster is one large detection box with a class/score tab (`SHILL · 31 ACCOUNTS · 94 S`).
- The verdict arrives as the banner itself: painting, detection boxes, the bronze coin, and the verdict
  as a wide wordmark.
- Type: Michroma (wide wordmark, like JUNCTION), Inter Tight (text), JetBrains Mono (`//` labels, data,
  terminal). Black, white, CV green, and the painting's own colours for the verdicts.

## What's on screen

- **Canvas** (`src/engine/graph.ts`): DOM cards over two canvas layers. Custom physics: pairwise
  repulsion, rectangle collisions, parent/child springs, and in *Timeline* mode every card is
  anchored to its time. *Web* mode drops the time anchor for a free force layout.
  - Time axis: gaps grow with `log(gap)`, so seconds-apart bursts and years-apart hops fit on one
    axis. Gaps over 3 days are marked as compressed ("2.7 yr later"). Posts less than 2 min apart
    become a time-ordered grid, so a 31-account burst reads as one block.
  - Drag a card: it follows the cursor elastically, tilts with its velocity, keeps its fling, and
    snaps back to its moment in time. Pan with inertia, wheel/pinch zoom around the cursor, double
    click to focus, double click the background to fit.
  - Semantic zoom: below ~0.2× cards fade into small detection boxes with labels.
  - Hover or select a card: its lineage (ancestors and descendants) lights up and particles flow
    along its edges. Clusters get a breathing hull labelled with the detector's numbers.
- **Trackdown run** (`runTrace` in `src/main.ts`): the submitted card is scanned, then each step
  of `case.steps` reveals what it found (rejected candidates flash up as ghost cards), the camera
  follows, the origin gets a shockwave, the rest of the spread pours in, and the verdict coin lands.
- **Timeline dock** (`src/ui/dock.ts`): histogram on the same compressed axis, scrub or play the
  spread, ruler across the top, minimap.
- **Receipts panel** (`src/ui/panel.ts`): verdict, trail to the source, evidence by tool
  (Detector / Nemotron VL / Oxylabs / Gemini), counter-evidence, "what would change our mind",
  and the selected card's details.

Keys: `N` trace a post · `Space` play/pause · `F` fit · `T` timeline/web · `←/→` parent/child ·
`↑/↓` siblings · `1-9` open a case · `Esc` skip/close.

## Data and honesty

- **Archive cases** (`src/data/cases.ts`) are simulated: fictional accounts, `.example` domains,
  and every one is badged *Simulated* in the header, the verdict card and the trace console. Their
  numbers come from the simulation, not from `eval/` or `signals/`.
- **Dürer's Rhinoceros** is real history, written by hand from public sources and badged
  *Historical record*. It is not scored and was not run through the detector. Its one present-day
  repost is marked as illustrative. It shows the honesty point: a picture can be wrong without its
  spread being coordinated.
- **Live clusters**: when `GET /api/clusters` answers, they're listed under "From the monitor" and
  converted by `liveCase()` (`src/data/api.ts`) from the `ClusterDetail` shape in
  `api/schemas.py`. Status, confidence, evidence and source URLs are shown as the API returns
  them. Parent links are inferred: a post's parent is the earliest earlier post sharing its link,
  image hash or normalised text.
- **Trace a post** calls `POST /api/lookup` with `{url}` for HTTP(S) links or `{text}` otherwise,
  then polls `GET /api/lookup/{id}` every 1.5 seconds for up to 150 seconds. Each response updates
  the step console, cards and receipts. The verdict coin is revealed only for a completed job;
  failures and timeouts leave partial receipts available without a final verdict. Starting another
  trace cancels the previous client polling. All API paths are absolute so `/trackdown/` works.
- **Real lookup data**: posts, platform engagement snapshots, profile facts, linked pages and
  their exact timestamps come from the backend contract in `lookup/README.md`. Selected cards
  show source URLs and receipt ids, plus each receipt's endpoint, latency and request id. X
  snowflake timestamps are labelled "time from post id". Missing facts stay unknown; cards with
  missing times use the submitted time (or lookup start) for layout only and say "time unknown".
  Nemotron descriptions/OCR and the cited Gemini reading are model inferences, not verified facts.
- **Samples, not a census**: copy-search results and recent profile posts are sampled. Spread
  counts, first-hour accounts, times and gaps describe only that sample; notes stay visible.
  A group of three or more accounts copying text is labelled as a copy sample with `insufficient`
  status, never a coordination verdict. Only a matched monitored cluster supplies the detector
  status. Cluster summaries do not identify memberships, so no lookup account is silently assigned
  to a monitored cluster. If several match, the first returned cluster supplies the headline status;
  all matched statuses remain visible in Provenance. Otherwise coordination says "Can't tell (yet)".
- **Fallbacks**: an unavailable lookup (404/503/network) tries `POST /api/analyze`; only if that
  path is also unavailable does the app run a clearly labelled simulated trace. Bad input (400),
  rate limits (429), job failure and timeout show an error instead of silently inventing a trace.
- **Hosting**: `npm run ship` refreshes `web/public/trackdown/`. Caddy redirects `/trackdown` to
  `/trackdown/` and uses `/trackdown/index.html` for that app's fallback, not the main dashboard.
- Wording: "Shill" labels a cluster's spread. Cards in a cluster are called "In coordinated
  cluster"; no account is ever called a shill.

## Artwork

All public domain, from Wikimedia Commons: Albrecht Dürer, *Rhinocerus* (woodcut, 1515); Sandro
Botticelli, *The Birth of Venus* (c. 1485; top band cropped, darkened, with ORB keypoints). The
"copy" plates in the Rhinoceros case are the same woodcut, coarsened, and are captioned as stand-ins.


## Verdict coin

Rendered in Blender 4.2 (Cycles, CPU) from `blender/coin.py`: struck bronze, a cinnabar SHILL
field with a flag, a verdigris CHILL field with sunglasses, and the legend
"SHILL OR CHILL · FACTA NON VERBA · MMXXVI" in Cinzel round the rim, 48-frame flip packed into `public/coin/coin-sheet.webp` by
`blender/pack.py`. Shill lands on frame 0, Chill on frame 24, and "Can't tell (yet)" lands on
its edge and wobbles. The rail and panel use stills; the abstain still is a motion-blurred,
still-spinning coin. The font is Cinzel (OFL).
