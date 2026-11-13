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

URL flags: `?case=tap-water|bridge|flood|glow` picks an archive case, `?static` skips the intro run.

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

## Verdict coin

Rendered in Blender 4.2 (Cycles, CPU) from `blender/coin.py`: brass coin, enamel SHILL face with
a flag, CHILL face with sunglasses, 48-frame flip packed into `public/coin/coin-sheet.webp` by
`blender/pack.py`. Shill lands on frame 0, Chill on frame 24, and "Can't tell (yet)" lands on
its edge and wobbles. The rail and panel use stills; the abstain still is a motion-blurred,
still-spinning coin. The font is Archivo Black (OFL).
