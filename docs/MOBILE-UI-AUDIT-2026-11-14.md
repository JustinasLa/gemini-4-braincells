# Mobile UI audit - 2026-11-14

## Scope and method

Read-only responsive UI audit of the Trackdown app in a Vite preview. Nine independent viewport checks covered the six leading Finnish mobile CSS resolutions reported for October 2026 by [StatCounter](https://gs.statcounter.com/screen-resolution-stats/mobile/finland) plus compact and wide boundary sizes. Each viewport was checked for horizontal overflow, header controls and Explore menu placement; sample selection and tracing/settled map states were checked where noted. This used a desktop browser's responsive viewport, not physical iOS/Android devices or browser engines.

The Finnish top six used for coverage were 414x896 (15.36%), 384x832 (11.57%), 390x844 (8.96%), 393x873 (7.47%), 360x800 (5.22%), and 360x780 (5.00%).

## Results

| CSS viewport | Horizontal overflow | Trace loading / settled state | Other observations |
| --- | --- | --- | --- |
| 414x896 | None observed | Dock extended to y=912, 16px below viewport while tracing; fits after loading | Explore menu stays in viewport; small targets below |
| 384x832 | None (scrollWidth 384) | Dock ended at y=848, 16px below viewport for at least 3.8s; fits after loading | Small targets below |
| 393x873 | None (scrollWidth 393) | Dock y=771-889, 16px below viewport while tracing; dock and verdict fit after loading | Menu links ~148x22px; controls below |
| 390x844 | None observed | Dock extended to y=860, 16px below viewport while tracing; fits after loading | Header controls and Explore menu fit |
| 360x800 | None (scrollWidth 360) | Dock extended 16px below viewport while tracing; fits after loading | Small targets below |
| 360x780 | None observed in settled state | Dock briefly extended to y=796, 16px below viewport; later fit | Header controls and Explore menu fit |
| 320x700 | None (document and body width 320px) | Sample loading/map state not checked | Explore menu links ~148x22px; Previous/Next ~30x30px; Simulated Examples control ~177x32px. Feed view returned HTTP 500 with the API unavailable in the Vite-only environment. |
| 375x812 | None (scrollWidth 375) | Dock extended to y=828, 16px below viewport while verdict loaded; fits after loading | Explore menu and header controls fit |
| 430x932 | None observed | Dock extended to y=948, 16px below viewport while tracing; fits after loading | Header controls and Explore menu fit |

Across tested sizes, the Explore button is 80x44 CSS px; Search and QR are 44x44px. The Explore menu stays inside the viewport. No settled-state overlap between the dock and verdict peek was observed. The 393x873 run reported no console errors.

## Findings and actions

### 1. Expand undersized touch targets - medium priority

Repeated measurements found Explore menu links at about 148x22px, timeline Previous/Next at 30x30px, Play at 42x42px, and the Simulated Examples disclosure at 177x32px. These targets are smaller than the 44px minimum used for this audit.

Actions:

- Give each link inside #judge-links a minimum 44px hit height and enough vertical padding; preserve the menu's current visual style and spacing.
- Increase .step hit boxes to at least 44x44px while keeping their arrow glyphs visually compact. Make .play at least 44x44px.
- Give the Simulated Examples summary control at least 44px of hit height without unnecessarily enlarging its text.
- Recheck the 320px boundary after changing the controls to ensure the dock readout still fits.

### 2. Keep the trace-loading dock inside the visible stage - medium priority

At every tested resolution where a sample was loaded, the bottom of the timeline dock was observed 16px below the viewport during TRACING. At 384x832 this persisted for at least 3.8 seconds. After loading, the dock reflowed into the visible viewport and did not overlap the verdict peek.

Actions:

- Adjust the mobile loading-state dock positioning/height in web/trackdown/src/styles.css so its full hit area remains within the visible .stage throughout TRACING as well as the settled phase.
- Account for the safe-area inset when calculating the dock's bottom position; verify on short-height screens as well as the six market-leading resolutions.
- Confirm users can reach every timeline control during tracing, not only after the layout changes on completion.

### 3. Repeat the feed route check with its API available - validation follow-up

At 320x700, /?view=feed showed its recovery UI but the local Vite-only run returned HTTP 500 from the unavailable API. This does not establish a mobile rendering defect.

Action: rerun the feed, metrics, and methodology routes against a running backend and record whether their narrow layouts and error states remain usable.

## Overall

No horizontal page overflow was found. The Explore header control is contained at the tested sizes. The repeatable issues are small touch targets and a temporary 16px timeline-dock clip during trace loading.
