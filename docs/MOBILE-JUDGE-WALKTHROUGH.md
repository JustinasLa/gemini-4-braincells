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

## Responsive check

Headless Chromium was run at 360x740 (Galaxy S8+), 375x667 (iPhone SE), 390x844 (iPhone 12 Pro), and 412x915 (Pixel 7). The home input fit the viewport at each size. The verdict and replay dock were measured with the app in its completed phase. The first run showed a 16 px overlap; after increasing the dock offset, all four viewport layout checks passed with no horizontal overflow or overlap. The local API was unavailable, so the sample did not progress to a real completed lookup and the live judge flow was not verified.
