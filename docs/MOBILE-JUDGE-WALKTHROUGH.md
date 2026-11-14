# Mobile judge walkthrough

This is a guide to the current judge routes and mobile controls. Follow the steps on the deployed Trackdown page; the live check record below lists only the checks performed for this integration.

## Walkthrough

1. Open **Explore** in Trackdown. Choose **Live cluster feed**, **Measured accuracy**, or **Methodology & limits**. The routes are also available directly as `?view=feed`, `?view=metrics`, and `?view=methodology`.
2. In **Live cluster feed**, inspect recent real detector clusters. The feed requests `/api/clusters?include_synthetic=false&limit=50`; synthetic evaluation clusters are excluded. **Open receipts** opens the selected cluster in Trackdown. The Atom feed is linked from this page.
3. In **Measured accuracy**, review the published evaluation rows from `/api/metrics`. Precision, recall, F1, and false-positive rate are shown when present.
4. In **Methodology & limits**, review the dataset, split, metric, and detector limitations.
5. To inspect a cluster on a phone, tap its verdict strip to open **Verdict and evidence**. Use the available tabs and close control to return to the map. **Chain** appears only when a chain can be built; **Post** appears after selecting a map card.
6. To try an input, return to Trackdown home, enter a post, URL, or @handle, and tap **Trace it**. A detector status applies only when the lookup matches a monitored cluster; web findings alone are not a coordination verdict.
7. Use the **QR** button beside **New trace** to display a handoff code for opening the page on a phone.

## Live check record

Checked 2026-11-14 against `https://noise-radar.srv2159172.hstgr.cloud`. The deployment's `/version.txt` returned `e987a83d4da3`.

| Request | Result |
|---|---|
| `GET /` | HTTP 200, HTML |
| `GET /trackdown/` | HTTP 200, HTML |
| `GET /api/clusters?include_synthetic=false&limit=50` | HTTP 200, JSON response |
| `GET /api/metrics` | HTTP 200, JSON response |
| `GET /version.txt` | HTTP 200, text |

No phone-sized browser session or tap sequence was performed for this check. The mobile viewport layout, Explore menu, verdict sheet, and QR dialog have not been live-verified here.

## Known limits

- Feed and metrics data can change or be empty when revisited.
- A detector verdict is available only for a matched monitored cluster.
- Synthetic campaign recall is not recall on real campaigns; live organic false-positive labels are heuristic; live precision and confidence calibration require human labels.
