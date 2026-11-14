# Mobile judge walkthrough

## Judge routes

- **Live cluster feed:** `?view=feed`
- **Measured accuracy:** `?view=metrics`
- **Methodology and limits:** `?view=methodology`
- **Try a post:** `?view=judge`

The Explore menu provides the judge destinations. A judge can enter a post, URL, or @handle and start a trace. A detector status applies only when the lookup matches a monitored cluster; web findings alone are not a coordination verdict.

## Phone flow

1. Open Explore and choose a judge destination, or open one of the routes above directly.
2. In the feed, open a cluster's receipts to view it in Trackdown.
3. Tap the verdict strip to open the verdict and evidence sheet. Inspect its tabs, then close the sheet to return to the spread map.
4. Use the replay controls below the verdict card to play or scrub the timeline. The phone layout separates the verdict card, timeline scrubber, and map controls; long titles and timestamps wrap within the viewport.
5. Return home, enter a post, URL, or @handle, then tap **Trace it**. Use the QR button beside it to hand the current page to a phone.

## Verification status

The mobile source and judge route are consolidated in this branch. The phone viewport and live judge flow still need a smoke check after deployment. Earlier mobile audit notes are retained separately and should not be treated as validation of this branch's current CSS.
