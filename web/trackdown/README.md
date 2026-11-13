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
```

URL flags: `?case=tap-water|rhinoceros|bridge|flood|glow` picks an archive case, `?static` skips the intro run.

## Look

Junction 2026's theme is *European Tech Renaissance*, so the trackdown is drawn as a Renaissance
provenance study (how art historians trace a work back to the original and spot copies), set
inside Junction's black chrome.

- Canvas: vellum with a ruled codex grid. Posts are paper folios with catalogue numbers; cluster
  members are slips with a cinnabar margin. Links are ink lines drawn in by a nib; compressed
  stretches of time are hatched bands. Clusters are engraved with hatching and get a figure caption.
- Pigments, not neon: cinnabar = Shill, verdigris = Chill, slate = Can't tell, gold leaf = the
  original, sienna = altered copy, lapis = found by search.
- Type: EB Garamond (reading), Cinzel (inscriptions, numerals), IBM Plex Mono (Junction's `//` labels,
  data, the trace terminal).
- Chrome: top bar, dock, modal and trace log in Junction black with chamfered corners; the splash and
  the modal use a pixel-sorted Botticelli, like the Junction site's glitched classical hero.
- Motion: the nib draws each link, a compass circle is struck around each find, rejected candidates
  are pencilled in and struck through, the burst's engraving fills in, the bronze coin is tossed.

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
  - Semantic zoom: below ~0.2× cards fade into dots with labels.
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
- **Live clusters**: when `GET api/clusters` answers, they're listed under "From the monitor" and
  converted by `liveCase()` (`src/data/api.ts`) from the `ClusterDetail` shape in
  `api/schemas.py`. Status, confidence, evidence and source URLs are shown as the API returns
  them. Parent links are inferred: a post's parent is the earliest earlier post sharing its link,
  image hash or normalised text.
- **Trace a post** calls `POST api/analyze`. `matched` opens that cluster; `insufficient` shows
  the web findings as source cards with no verdict. If the API isn't reachable, the app runs a
  simulated trace with the user's text and says so (toast, badge, verdict text).
- Wording: "Shill" labels a cluster's spread. Cards in a cluster are called "In coordinated
  cluster"; no account is ever called a shill.

## Artwork

All public domain, from Wikimedia Commons: Albrecht Dürer, *Rhinocerus* (woodcut, 1515); Sandro
Botticelli, *The Birth of Venus* (c. 1485; cropped to head and shoulders, pixel-sorted). The
"copy" plates in the Rhinoceros case are the same woodcut, coarsened, and are captioned as stand-ins.
The vellum texture is generated.

## Verdict coin

Rendered in Blender 4.2 (Cycles, CPU) from `blender/coin.py`: struck bronze, a cinnabar SHILL
field with a flag, a verdigris CHILL field with sunglasses, and the legend
"SHILL OR CHILL · FACTA NON VERBA · MMXXVI" in Cinzel round the rim, 48-frame flip packed into `public/coin/coin-sheet.webp` by
`blender/pack.py`. Shill lands on frame 0, Chill on frame 24, and "Can't tell (yet)" lands on
its edge and wobbles. The rail and panel use stills; the abstain still is a motion-blurred,
still-spinning coin. The font is Cinzel (OFL).
