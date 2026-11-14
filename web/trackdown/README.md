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
npm run check                       # lookup rendering regression (fixture -> lookupCase -> cards)
```

URL flags: `?case=tap-water|rhinoceros|bridge|flood|glow` picks an archive case, `?static` skips the intro run.
`?lookup=<URL-encoded link>` starts a live lookup. An image (a meme, a screenshot) starts an image
lookup from the home screen's Upload image button, by dropping it on the home screen or by pasting
it into the input (`POST /api/lookup/image`, lookup/README.md); photos over 2 MB or 2048 px are sent
as a 2048 px JPEG. In the Vite development server only,
`?mockLookup` runs a progressive fictional fixture; the header, panel and console label it MOCK.
The fixture is excluded from production builds and never runs by default.

## Look

One visual language, taken from Junction 2026's banner: a Renaissance painting read by machine vision.

- The canvas sits on Botticelli's *Birth of Venus*, darkened, with its real ORB detections baked in
  (green boxes, match scores, dashed match lines), drifting slightly as you pan.
- The nudity in `venus-full.webp` is smudged to smooth skin (`blender/cover_venus.py`, run once on the
  uncovered painting); on the home page, pinned detection boxes (`"pin": true` in `venus-full-cv.json`)
  sit over those spots and show warped drapery like every other box.
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
    axis. Gaps over 3 days are marked on the ruler and dock ("2.7 yr gap"). Posts less than 2 min apart
    become a time-ordered grid, so a 31-account burst reads as one block.
  - Drag a card: it follows the cursor elastically, tilts with its velocity, keeps its fling, and
    snaps back to its moment in time. Pan with inertia, wheel/pinch zoom around the cursor, double
    click to focus, double click the background to fit.
  - Semantic zoom: cards stay up as you zoom out; only near the zoom-out limit (under ~0.15×)
    each post becomes a labelled marker in its role colour; cluster members are small squares
    inside the cluster box, whose tab (verdict + account count) stays 11px on screen.
    Labels avoid each other, markers and tabs.
  - Hover or select a card: its lineage (ancestors and descendants) lights up and particles flow
    along its edges. Clusters get a breathing hull labelled with the detector's numbers.
- **Trackdown run** (`runTrace` in `src/main.ts`): the submitted card is scanned, then each step
  of `case.steps` reveals what it found (rejected candidates flash up as ghost cards), the camera
  follows, the origin gets a shockwave, the rest of the spread pours in, and the verdict coin lands.
- **Timeline dock** (`src/ui/dock.ts`): histogram on the same compressed axis, scrub or play the
  spread (also a native range input for keyboards), ruler across the top, minimap.
- **Panel** (`src/ui/panel.ts`): tabs Verdict / Chain / Evidence / Post. Evidence is grouped as
  "Supports the verdict" and "Signs of organic spread" (or "Evidence considered" for Can't tell),
  plus "what would change our mind". On phones it is a modal sheet opened from the verdict bar.

Keys: `N` trace a post · `Space` play/pause · `F` fit · `T` timeline/web · `M` key and map · with the
map focused `←/→` earlier/later post, `↑/↓` siblings · `1-9` open a case · `Esc` skip/close.
Phones: Fit, TIME/WEB and KEY sit in the map toolbar.

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
- **Trace a post** calls `POST /api/lookup` with `{url}` for HTTP(S) links or `{text}` otherwise (the API looks up an `@handle` as that Bluesky profile),
  then polls `GET /api/lookup/{id}` every 1.5 seconds for up to 150 seconds. Each response updates
  the step console, cards and receipts. The verdict coin is revealed only for a completed job;
  failures and timeouts leave partial receipts available without a final verdict. Starting another
  trace cancels the previous client polling. All API paths are absolute so the app works at `/` and `/trackdown/`.
- **Real lookup data**: posts, platform engagement snapshots, profile facts, linked pages and
  their exact timestamps come from the backend contract in `lookup/README.md`. Selected cards
  show source URLs and receipt ids, plus each receipt's endpoint, latency and request id. X
  snowflake timestamps are labelled "time from post id". Missing facts stay unknown; cards with
  missing times use the submitted time (or lookup start) for layout only and say "time unknown".
  Nemotron descriptions/OCR and the cited Gemini reading are model inferences, not verified facts.
- **Screenshots of posts**: a post the lookup found because a screenshot in the submitted post shows
  it has role `screenshotted` ("Real post in the screenshot", lilac, dashed card). Its `screenshot_of`
  link is drawn as a dotted lilac line and kept out of the copy chain, the path to the earliest copy,
  copy samples, spread counts and the histogram. Each `screenshots[]` receipt becomes a
  "Screenshot check" data group (matches / differs / no matching post found / not checked, time
  note, differences, counts as context only) on the real post and on the post carrying the
  screenshot, plus a flag on that post, so a receipt with no real post found is still visible. The
  wording describes the screenshot, never a person. Unknown roles or link types from a newer API
  render as neutral cards and lines instead of failing.
- **Samples, not a census**: copy-search results and recent profile posts are sampled. Spread
  counts, first-hour accounts, times and gaps describe only that sample; notes stay visible.
  A group of three or more accounts copying text is labelled as a copy sample with `insufficient`
  status, never a coordination verdict. Only a matched monitored cluster supplies the detector
  status. Cluster summaries do not identify memberships, so no lookup account is silently assigned
  to a monitored cluster. If several match, the first returned cluster supplies the headline status;
  all matched statuses remain visible in Provenance. Otherwise coordination says "Can't tell (yet)".
- **Factual findings**: the lookup's Findings tab shows claim-level model assessments and
  clickable source receipts separately from the coordination coin and its confidence. A missing
  assessment is not treated as false content. Media descriptions do not authenticate the exact
  image, and the earliest observed copy is not necessarily the original source. Older API
  responses without `claim_checks` show an explicit unavailable-assessment message.
  Run `npm test` for rendering, citation safety, legacy fallback, and verdict-separation tests.
- **Fallbacks**: an unavailable lookup (404/503/network) tries `POST /api/analyze`; only if that
  path is also unavailable does the app run a clearly labelled simulated trace. Bad input (400),
  rate limits (429), job failure and timeout show an error instead of silently inventing a trace.
- **Hosting**: `npm run ship` refreshes `web/public/trackdown/`. Caddy serves it as the site's home
  page at `/` (and still at `/trackdown/` for old links). `/?cluster=<id>` opens a live cluster; the
  removed dashboard's links (`/monitor/...`, `/#/cluster/7`) land there or on the home page.
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

## Meme trace (image matches)

Optional `image_matches` add an earliest-match Original card and grouped variations to the map,
timeline and panel. Select a variation to compare the submission with it (side by side or a keyboard
slider; slider on phones). Image dates say “first seen by TinEye” (an upper bound, not publication),
“page dated”, “time from post id”, or “date unknown”; an upload never becomes its own origin.
Descriptions are Nemotron’s reading, not verified facts. Say “edited”, “changed”, “variation”, never
“fake”, “deepfake”, or claims about intent. Different-looking matches are not counted as variations.
Dev-only `?mockLookup=meme`, `?mockLookup=meme-empty`, `?mockLookup=meme-failed`, and
`?mockLookup=meme-blocked` show fictional `.example` results and self-contained images, including
progressive searching/comparing states. The mock submission is a link with a local image; it needs
no backend. `?mockLookup=meme-loading` holds each loading phase for 15 seconds for inspection.
All mocks remain dev-only and labelled MOCK. Run `npm run check:meme` for pure checks.
