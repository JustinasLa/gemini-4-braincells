# Mobile judge walkthrough

This walkthrough covers the deployed Trackdown interface and the mobile judge destinations.

## Walkthrough

1. **Open the judging destinations.** From Trackdown, tap **Explore** in the top bar. Choose **Live cluster feed**, **Measured accuracy**, or **Methodology & limits**. These views can also be opened directly with `?view=feed`, `?view=metrics`, and `?view=methodology`.
2. **Browse clusters.** Live cluster feed loads real detector clusters from `/api/clusters?include_synthetic=false&limit=50`; synthetic evaluation clusters are excluded. Each row links to **Open receipts**, which opens Trackdown with `?cluster=<id>`. The Atom feed is linked from the page. If the API returns no rows, there are no clusters to browse at that moment.
3. **Inspect a cluster on the map.** On a phone, tap the verdict strip at the bottom to open the **Verdict and evidence** sheet. The sheet has a Verdict tab and may show Chain when a chain exists, Evidence for collected receipts and source/media/analyst evidence, and Post after selecting a map card. Use the sheet close control to return to the map. Compare coordinated and likely-organic examples when both are available.
4. **Review metrics and limits.** **Measured accuracy** loads the published evaluation rows from `/api/metrics`. The view displays precision, recall, F1 and false-positive rate when reported. **Methodology & limits** explains the datasets, splits, metric definitions and known limitations. An empty metrics response means no published rows are available.
5. **Try a judge input.** Return to Trackdown home, enter a post, URL or @handle, and tap **Trace it**. The lookup shows collected receipts. A detector status applies only when the lookup matches a monitored cluster; web findings alone are not a coordination verdict. The older `/api/analyze` API remains available as a fallback and does not create a new detector verdict for unmatched input.
6. **Try phone handoff.** The **QR** button beside **New trace** opens a QR code for the current page. Scan it with a phone to continue there.

## Viewport walkthrough

The mobile layout was exercised on 2026-11-14 at each device preset. At each size, the local simulated "The weekend tap-water ban" case opened the verdict sheet; Chain and Evidence tabs switched; and the sheet close control returned to the map. The Explore menu and Methodology route were also checked. The QR control measured 44x44 CSS pixels at all four widths; its dialog opened and closed at Galaxy S8+ size.

| Device preset | CSS viewport | Result |
|---|---:|---|
| iPhone SE | 375x667 | Passed local sample, verdict sheet, Chain/Evidence tabs, and close. QR target 44x44. |
| iPhone 12 Pro | 390x844 | Passed local sample, verdict sheet, Chain/Evidence tabs, and close. QR target 44x44. |
| Pixel 7 | 412x915 | Passed local sample, verdict sheet, Chain/Evidence tabs, and close. QR target 44x44. |
| Galaxy S8+ | 360x740 | Passed local sample, verdict sheet, Chain/Evidence tabs, close, and QR dialog open/close. QR target 44x44. |

Feed and Metrics routes rendered their unavailable-data state in this local run because no API backend was running on the Vite proxy target. Their live data and cluster-opening links therefore were not exercised. Use a backend with current cluster data to complete that part of the live judge flow. Chain appears only when a chain can be built; Post appears after selecting a map card. Record the actual CSS viewport dimensions reported by the browser.

## Known limits

- Feed and metrics views require the corresponding live API endpoints and can be empty.
- Chain appears only when a chain can be built; Post appears after selecting a card.
- Judge input is a lookup of public receipts. A detector verdict is available only for a matched monitored cluster.
- Synthetic campaign recall is not recall on real campaigns; live organic false-positive labels are heuristic; live precision and confidence calibration require human labels.
