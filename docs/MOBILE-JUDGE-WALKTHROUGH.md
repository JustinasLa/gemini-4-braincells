# Mobile judge walkthrough

This guide describes the judge destinations and phone controls in the current Trackdown source. The deployed site may differ until this worktree is released.

## Walkthrough

1. Open **Explore** in Trackdown. Choose **Live cluster feed**, **Measured accuracy**, **Methodology & limits**, or **Try a post**. Direct destinations are `?view=feed`, `?view=metrics`, `?view=methodology`, and `?view=judge`.
2. In **Live cluster feed**, inspect recent real detector clusters. The page requests `/api/clusters?include_synthetic=false&limit=50`, excluding synthetic evaluation clusters. **Open receipts** opens the cluster in Trackdown; the Atom feed is linked from the page.
3. In **Measured accuracy**, review published evaluation rows from `/api/metrics`. Precision, recall, F1, and false-positive rate are shown when reported.
4. In **Methodology & limits**, review the dataset, split, metric, and detector limitations.
5. Choose **Try a post** or return to Trackdown home. Enter a post, URL, or @handle and tap **Trace it**. A detector status applies only when the lookup matches a monitored cluster; web findings alone are not a coordination verdict.
6. To open the page on a phone, use the **QR** button beside **New trace**.
7. To inspect a cluster on a phone, tap its verdict strip to open **Verdict and evidence**. Use the available tabs and close control to return to the map. **Chain** appears only when a chain can be built; **Post** appears after selecting a map card.

## Verification

The Trackdown source and shipped assets built successfully in this worktree with `npm run ship`. A narrow CSS viewport and tap sequence were not verified: the browser automation host did not provide a reliable viewport for this run, and the local API service was unavailable. A phone-width visual and touch-flow check remains necessary before calling mobile support verified.

## Historical deployment check

A prior check on 2026-11-14 against `https://noise-radar.srv2159172.hstgr.cloud` observed `/version.txt` = `e987a83d4da3`; `/`, `/trackdown/`, `/api/clusters?include_synthetic=false&limit=50`, `/api/metrics`, and `/version.txt` returned HTTP 200. That check predates this worktree and does not verify its CSS change or the current deployment.

## Known limits

- Feed and metrics data can change or be empty when revisited.
- A detector verdict is available only for a matched monitored cluster.
- Synthetic campaign recall is not recall on real campaigns; live organic false-positive labels are heuristic; live precision and confidence calibration require human labels.
