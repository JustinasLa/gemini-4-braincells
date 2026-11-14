# Worldwide mobile UI audit - 2026-11-14

## Scope and coverage

Read-only rendered responsive checks on Trackdown at four CSS viewport sizes not included in the previous audit: 412x915, 385x854, 384x854, and 412x892.

The selection completes the top 10 worldwide mobile resolutions. The latest full top-10 ranking available for this audit is StatCounter's September 2026 dataset, reproduced with its shares in this [resolution table](https://responsivetool.com/blog/common-screen-resolutions): top 10 sum to 51.27% of mobile page views. The six top-ranked sizes were already covered in the prior audit and were excluded here. StatCounter's latest October page still lists the same six leading sizes; the additional four tested here are ranks 7-10 in the latest complete top-10 table. This round tested origin/main 77e50a57; the prior six were tested on the older be629db snapshot. This is page-view share, not a count of unique users, and the remaining long tail is outside this top-10 coverage cohort.

Tested app code: origin/main 77e50a57aa7887f199af81b82ddbf9f9c766ad1e. Each resolution was checked in an independent rendered browser tab. Checks covered horizontal overflow, header and Explore menu, QR dialog, simulated example selection, tracing and settled map, timeline controls, verdict panel, and console errors where available. This was browser responsive emulation, not physical devices.

## Results

| Rank | CSS viewport | Sep 2026 share | Horizontal overflow | Findings |
| --- | --- | ---: | --- | --- |
| 7 | 412x915 | 2.99% | None observed | Explore menu fits. Explore/Search/QR are 80x44 / 44x44 / 44x44px. Menu links ~148x22px; Previous/Next 30x30px; Play 42x42px; Simulated Examples ~152x33px. During tracing dock bottom reached y=931 and stayed clipped 16px for at least 5s; it fit after loading. No console errors. |
| 8 | 385x854 | 2.76% | None (document width 385px) | Explore menu and QR dialog fit. Menu links ~148x21.75px; Previous/Next 30x30px; Play 42x42px; Simulated Examples ~152x33px. Tracing dock reached y=870, 16px below viewport; it fit after loading without verdict overlap. No console errors. |
| 9 | 384x854 | 2.02% | None (document width 384px) | Explore menu and QR dialog fit. Menu links ~148x21.75px; Previous/Next 30x30px; Play 42x42px; Simulated Examples ~352x33px. Tracing dock bottom reached y=870, 16px below viewport; settled dock and verdict fit without overlap. No console errors. |
| 10 | 412x892 | 1.92% | None (document scrollWidth 412px) | Explore menu fits. Explore/Search/QR are 80x44 / 44x44 / 44x44px. Menu links ~148x21.75px; Previous/Next 30x30px; Play 42x42px; Simulated Examples ~380x33px. Tracing dock reached y=908 and remained clipped in loading snapshots; settled dock and verdict fit. No console errors. |

The top-10 cohort totals 51.27%. The four newly tested sizes account for 9.69 percentage points of the September mobile page-view share. No exact viewport from the previous test matrix was repeated.

## Findings and actions

### 1. Increase tap areas below 44px - medium priority

All four newly tested resolutions reproduce the small tap targets from the previous audit:

- Explore dropdown links: about 148x22px.
- Timeline Previous/Next: 30x30px.
- Timeline Play: 42x42px.
- Simulated Examples summary: about 152-380x33px.

Actions:

- Give each #judge-links link at least 44px hit height and adequate vertical padding.
- Make .step and .play controls at least 44x44px while keeping their icons compact.
- Give .home-ex summary at least 44px hit height.
- Recheck the narrow 320px boundary after these changes.

### 2. Keep the tracing dock inside the visible stage - medium priority

The dock extended exactly 16px below the viewport during tracing at all four sizes. At 412x915 it remained clipped for at least five seconds, and at 412x892 it remained clipped through multiple loading snapshots. The dock and verdict fit after tracing completed.

Actions:

- Adjust the tracing-state mobile dock layout/position in web/trackdown/src/styles.css so the full dock and its controls remain inside the visible stage before the settled-phase layout applies.
- Include the mobile safe-area inset in the available bottom space.
- After the fix, recheck all four sizes here and the previous matrix, including the tracing state and settled verdict state.

### 3. Keep the existing fit and overflow behavior

All four sizes had no horizontal document overflow. The Explore control, dropdown, and QR dialog stayed within the viewport. Keep these passes intact while increasing touch areas and adjusting the dock.

## Overall

Across the prior and current audit rounds, each of the top 10 worldwide viewport sizes has been tested at least once, covering 51.27% in the latest complete September 2026 ranking, without repeating a viewport. The previous six sizes were tested on an older app snapshot; rerun all ten against one current build after the fixes. The same two actionable issues persist at every newly tested size: undersized tap targets and a 16px tracing-state dock clip.
