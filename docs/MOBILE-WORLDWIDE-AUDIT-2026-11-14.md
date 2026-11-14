# Worldwide mobile UI audit - 2026-11-14

## Coverage

StatCounter October 2026 ranks 20 worldwide mobile resolutions (64.70% combined page-view share) and aggregates the remaining 35.29% as Other. Thus the public exact-resolution dataset does not support a defensible 75% claim. All top-20 sizes have now been checked at least once across this and prior rounds, but several earlier sizes used a different app snapshot. Checks were responsive browser emulation, not physical devices.

Source: [StatCounter mobile resolution chart](https://gs.statcounter.com/screen-resolution-stats/mobile/worldwide), [October 2026 CSV](https://gs.statcounter.com/chart.php?resolution-ww-monthly-202610-202610&device_hidden=mobile&csv=1).

## New, non-overlapping sizes checked

| Rank | Viewport | Share | Finding |
|---|---|---:|---|
|11|360x806|1.82%|No horizontal overflow; welcome overlay stayed in front, settled result not reached. Prev/Next 30x30px, Play 42x42px.|
|12|360x820|1.58%|No horizontal overflow; sample settled; dock clipped 16px below viewport.|
|13|360x804|1.40%|Verdict dock started below viewport; fit/key rail extended to x=412 on 360px viewport.|
|14|393x852|1.38%|No overflow; welcome overlay stayed in front; dock clipped 16px.|
|15|375x667|1.36%|No overflow; Skip did not dismiss welcome overlay; dock clipped 16px.|
|17|360x802|1.28%|Sample settled; verdict began at y=802.7, below viewport. Rail fit.|
|18|384x857|1.21%|Tracing persisted across several seconds; verdict tab/Close below viewport; dock clipped 16px and Skip overlapped it. No console/network errors.|
|19|402x874|1.10%|Sample settled to CHILL / Organic spread / 84%; details began at y=875. Prev/Next 30px, Play 42px.|
|20|393x876|1.02%|Sample settled; verdict panel began at y=877, details/Replay required scrolling.|

No horizontal document overflow or console errors were reported on the sizes where settled flow was reached. Main Explore/Search/QR hit areas measured 80x44 / 44x44 / 44x44px.

## Combined findings and fixes

1. **P1 - Keep dock and verdict actions reachable on short screens.** The tracing dock commonly protrudes 16px; settled verdict controls are below the fold at 375x667, 360x802, 384x857, 393x876, and 402x874. At 384x857 Skip overlaps the dock. Adjust stage sizing and positioning for dynamic viewport height and safe-area insets; keep playback, verdict, and close/tab actions visible in tracing and settled states.
2. **P1 - Constrain floating rail.** At 360x804 the rail extended past the right edge despite no document overflow. Bound it to usable stage width and inspect splash/tracing/settled states at 320px and 360px.
3. **P2 - Enlarge touch targets.** Explore links are ~148x22px, Previous/Next 30x30px, Play 42x42px, and Simulated Examples summary ~33px high. Raise interactive areas to at least 44px without enlarging icons.
4. **P2 - Verify welcome Skip at short heights.** Skip did not dismiss at 375x667; welcome remained in front at 360x806 and 393x852. Confirm whether this is state/automation dependent; ensure Skip is reachable and doesn't overlap the dock.
5. **P2 - Recheck tracing completion at 384x857.** One sample remained Tracing while map posts were visible; reproduce in a fresh session and inspect completion handling if repeated.

## Next steps

To reach 75%, obtain aggregate first-party CSS viewport dimensions or a source with exact ranks beyond top 20; do not count Other as tested. After fixes, rerun the full top-20 matrix against a single current build, then add first-party resolutions until measured cumulative coverage exceeds 75%.
