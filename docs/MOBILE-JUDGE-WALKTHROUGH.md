# Mobile judge walkthrough

Live site: https://noise-radar.srv2159172.hstgr.cloud/. The home screen is Trackdown. It is not a cluster dashboard; use the API links below to browse the monitor feed and metrics.

## Walkthrough

1. **Open the cluster feed.** On a phone, open /api/clusters?status=coordinated&include_synthetic=false&limit=50 for current coordinated clusters. For likely-organic examples, change status=coordinated to status=likely_organic. The JSON route includes synthetic clusters by default, so this URL opts out; these responses are not a card feed in the site. The Atom alternative is /api/feed.atom?status=coordinated; change the status for likely-organic entries. The Atom feed is convenient in a feed reader and links entries to their cluster.
2. **Open one cluster.** Take its numeric id from the JSON and open https://noise-radar.srv2159172.hstgr.cloud/?cluster=<id> (replace <id> with the number). Trackdown draws the cluster's posts and spread. Tap the verdict strip at the bottom to open the mobile sheet. Its available tabs are Verdict, Chain when a chain exists, Evidence, and Post when a card is selected. Evidence includes signal receipts, source links, media findings and analyst evidence where present. Tap source URLs to inspect them; use the sheet close control to return to the map. Try one coordinated and one likely_organic cluster to compare suspicious signals with the organic counter-evidence.
3. **Find metrics and method.** Open /api/metrics for the published evaluation rows. There is no Accuracy or Metrics dashboard page in the current web app. The explanation of datasets, splits, metric definitions and limits is in docs/METHODOLOGY.md#how-the-metrics-are-made. An empty API response means no published run is available; live precision and confidence calibration require hand-labelled live clusters and are not reported until those labels exist.
4. **Try judge input.** Return to the site home, paste a post, URL or @handle into the input, then tap **Trace it**. The current UI starts /api/lookup and shows collected receipts; it does not present a dedicated /api/analyze form or guarantee a detector verdict for arbitrary input. A detector status applies only when the lookup matches a monitored cluster. Otherwise, treat source findings as web context, not a coordination verdict. The older /api/analyze API remains implemented as a fallback: matching input returns its monitored cluster(s); unmatched input returns insufficient plus optional live web findings, never a new coordination verdict.

## Phone viewport check

Checked 2026-11-14 against the live site. The browser preview accepted the configured sizes below, but its page runtime remained at 1280x800 CSS pixels (innerWidth/innerHeight); (max-width: 860px) stayed false. These are attempted configurations, not passing phone-layout tests.

| Configured viewport | Browser-reported viewport | Result |
|---|---:|---|
| 320x720 | 1280x800 | Not tested at phone width; mobile breakpoint did not activate. |
| iPhone 12 Pro, 390x844 | 1280x800 | Not tested at phone width; mobile breakpoint did not activate. |

The implementation switches to its phone layout at 860px and below. No phone-width visual or touch-flow pass is claimed from this check. Recheck these sizes in a viewport tool that applies its configured CSS dimensions before treating mobile layout as verified.

## Known limits for the demo

- Cluster browsing and evaluation metrics are API responses; the deployed web UI has no cluster-list or metrics page.
- Trackdown's bottom-sheet controls expose only the currently available evidence. A chain tab appears only when a chain can be built; Post appears after selecting a card.
- Judge input is a live lookup of public receipts. A detector verdict is available only for a matched monitored cluster; a Gemini assessment or web result is not itself the detector's status.
- Evaluation data has limits: synthetic campaign recall is not recall on real campaigns; live organic false-positive labels are heuristic; live precision and confidence calibration need human labels.
