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
4. Use the replay dock to play or scrub the timeline. The phone layout separates the verdict card, replay controls, and map controls, accounts for safe-area insets, and keeps controls within narrow viewports.
5. Return home, enter a post, URL, or @handle, then tap **Trace it**. Use **QR** to hand the current page to a phone.

## Verification status

The mobile source and judge route are consolidated in this branch. The phone viewport and live judge flow still need a smoke check after deployment. Earlier mobile audit and walkthrough notes describe checks against older revisions and should not be treated as validation of this branch.
