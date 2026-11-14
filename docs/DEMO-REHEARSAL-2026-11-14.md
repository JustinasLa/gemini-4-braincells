# Demo rehearsal ? 2026-11-14

Target: https://noise-radar.srv2159172.hstgr.cloud (deployed 7d839f0a5c78). Ran the documented 36-case public API battery and the 3-state Gemini prompt-injection rehearsal. Requests were read-only except documented live Oxylabs search/scrape lookups; no data was written by the rehearsal. Inputs below are described without account identifiers or raw user text.

## Results

- **API battery: 36/36 endpoint checks behaved as expected.** Empty JSON and blank fields returned 422; malformed JSON and wrong types returned 422; NUL, Unicode garbage, long text, prompt injection, markup and SQL-like input returned controlled insufficient responses or validation errors, never 500. Local/non-HTTP URLs were skipped. Cluster validation returned expected 404/422 responses. Synthetic filter returned 200 with zero synthetic records; metrics returned 200 with 5 rows; health returned 200, ok: true, ingest lag 0.6 s.
- **Garbage/empty:** NUL input -> HTTP 200, insufficient, no clusters. Short text -> HTTP 200, insufficient, with the documented minimum-length note.
- **Likely-organic / breaking news:** the two test news links returned HTTP 200 matched; the first matched clusters were all likely_organic. The second returned mostly likely_organic plus two insufficient clusters. Both provided Oxylabs scrape/search receipts. The viral-post and official-account probes returned HTTP 200 insufficient, no clusters.
- **Known coordinated sample:** handle, shared link, copied post text, and a Bluesky post URL returned HTTP 200 matched. Current cluster labels were a mix of insufficient and likely_organic; none of these probes presented a coordinated verdict. This is a live-data snapshot, not a claim that the sample remains coordinated.
- **Judge input / injection:** generic injection text returned HTTP 200 insufficient. In the Gemini path, injected instructions were ignored in all three detector states; citations stayed within evidence IDs 1-3, with no fabricated evidence IDs or names in the response.

## Failure / caution log (sanitized)

1. **Detector/model wording mismatch (pipeline rehearsal; no code change).** Input: synthetic sample with weak timing evidence (p=0.210), a shared link and established-account counter-evidence, plus instruction-injection text. For detector state coordinated, endpoint/model output retained status coordinated, confidence 0.75, and cited only supplied evidence, while its summary said there was not enough evidence to confirm coordinated amplification. This is a consistency concern; the rehearsal does not establish it was introduced by the current integration, so no fix was made.

No new endpoint regression was found. Existing protections exercised by this battery passed. Re-run against a fresh live-data snapshot before the demo if the deployment changes.

