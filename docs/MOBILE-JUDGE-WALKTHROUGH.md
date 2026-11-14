# Mobile judge walkthrough

This guide covers the mobile judge flow in Trackdown. The Explore menu provides the live cluster feed, measured accuracy, and methodology routes. A direct judge input starts from the home screen using a post, URL, or account handle. A detector verdict appears only when the input matches a monitored cluster; lookup findings alone are not a coordination verdict.

## Inspect a cluster on a phone

1. Open a cluster from the live feed, or choose a Trackdown example.
2. Tap the verdict strip to open the verdict and evidence sheet. Use the available tabs to inspect the verdict, evidence, chain when available, and selected post.
3. Close the sheet to return to the spread map.
4. Use the replay controls below the verdict card. The phone layout gives the verdict, timeline scrubber, and map controls separate screen areas; long titles and timestamps wrap within the viewport.
5. Use the QR button beside **New trace** to hand the current page to a phone.

## Judge input

1. Return to the home screen and enter a public post, URL, or @handle.
2. Tap **Trace it** and review the collected receipts.
3. If the lookup matches a monitored cluster, inspect its detector status and evidence. A web-only result does not establish coordination.

## Verification limits

The CSS and shipped Trackdown assets must be checked together when a release is built. This source update has not been verified in a phone-sized browser session. Confirm viewport fit, verdict-sheet open/close, replay controls, and QR handoff against the deployed release before describing the flow as live-verified.
