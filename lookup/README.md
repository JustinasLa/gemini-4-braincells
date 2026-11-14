# lookup/: trace one link through X, Bluesky and the web

A user pastes a link (an X post, a Bluesky post, a profile or any web page). The lookup collects
real, live data about it and returns it with a receipt for every fact:

- **The post itself** (FxTwitter's public API for X, falling back to Oxylabs `/scrape` of the X
  page; Bluesky public AppView): author, text, exact creation time, the counts (views, replies,
  reposts, likes, bookmarks), the linked card and the media. FxTwitter (`lookup/fxtwitter.py`,
  `api.fxtwitter.com/2/conversation/<id>`) serves X's own post data: it sees posts X's
  logged-out page hides (marked sensitive: "only available in the X app"), gives exact counts
  (zeros included) and the full text of long posts, in under a second. It is an unofficial
  community service, so the Oxylabs page read stays the fallback when it fails or is slow
  (`LOOKUP_FXTWITTER=false` turns it off), and receipts name which one read the post. A post
  FxTwitter says is gone is still read on its X page, which says why (suspended, protected,
  deleted, sensitive). Candidate copies are read the same way.
- **The author's history** (Oxylabs `/scrape` of the profile): join date, followers, following,
  post count, bio, and the recent posts the profile page shows, with their exact times.
- **Where the text appears** (Oxylabs `/search`, exact phrase, the open web plus `site:x.com`
  and `site:bsky.app`; Bluesky `searchPosts`): copies and quotes from other accounts, each with
  its own time. X post ids are snowflakes, so every X hit gets its creation time from the id
  alone (`(id >> 22) + 1288834974657` ms), no extra request.
- **The linked page and the coverage** (Oxylabs `/scrape` with AI extraction; `/search` for the
  headline / claim): publisher, date, headline, main claims; other outlets on the same story.
- **Media** (Nemotron 3.5 Super VL): description and OCR of the post's images; the OCR text is
  searched like the post text, so a screenshot that is passed around is found too.
- **Screenshots of posts** (Nemotron reads the screenshot; Bluesky API and Oxylabs find the
  real post): when one of the post's own images is a screenshot of a social post, the lookup
  looks for the real post it shows and says whether it was found and matches
  (`lookup/receipts.py`, run by `lookup/receipts_step.py`).
- **Our monitor**: whether the link, text or accounts are in a cluster the detector watches
  (`api/store.py`), and that cluster's detector status.
- **The reading.** Nemotron 3.5 Super VL (text-only chat on the Verda vLLM endpoint, reasoning
  off, guided JSON) writes a short, cited reading of the collected receipts in about 1.5 s:
  origin, corroboration, spread pattern, counter-evidence, what would change the reading.
  Gemini writes a second reading of the same receipts on the same stream (about 15-20 s later).
  Both may cite only receipt ids that exist (checked in code); neither sets the status. When
  their calls differ, both are shown and the difference is counted (`readings_disagree`), never
  resolved by picking one.

**Status rule (honesty):** `status` is `matched` only when the input hits a cluster we monitor;
the coordination verdict is then that cluster's detector status. Otherwise `status` is
`insufficient`: one link's web footprint is not proof of coordination, and the copy pattern is
shown as data (counts, times, accounts), never as a verdict. Search results are a sample, not a
census: the notes say so.

## Factual findings are separate from coordination

The assessment returns up to six `claim_checks`, each with `claim`, `finding`
(`supported`, `contradicted`, `mixed`, or `unverified`), `explanation`, and its own receipt
`cites`. These are model readings of collected source text, not detector verdicts or guarantees
of truth. An accurate claim can be amplified in a coordinated way; an inaccurate claim can
spread organically. `status` and `llm_reading` keep their coordination meanings.

Coverage preserves the broad headline/caption-opening search and adds a separate search
retaining up to 40 words of the claim. Both run concurrently, within the provider deadline;
a failed search does not discard the other's results.
The verification phase reads up to three source pages (`Deps.source_pages_max`) within the
existing provider deadline, prioritizing non-social reporting, government/academic records
and older candidate sources, and diversifying domains. One slot prioritizes the earliest
pre-post text-copy candidate to preserve origin discovery; it can start before coverage
arrives when at least one other slot remains for reporting. Other repost sites remain
fallback leads. Domain priority is a retrieval heuristic, not a guarantee
of credibility. Failed or unread pages remain search leads, with limitations shown in notes.
The prompt distinguishes read pages from search snippets and includes their source URLs.

A decisive claim finding needs a citation to a successfully read page with extracted claim
text, other than the submitted page itself. Unknown/failed receipt IDs are removed. Unsupported
findings are downgraded to `unverified`, with a cautious replacement summary. This guard checks
source availability, not semantic entailment: a model can still misinterpret a cited source.
The prompt requires primary-source preference, conflicting evidence, procedural context,
attributed estimates versus guaranteed results, and chronology versus causation.

When source pages document media attribution, `assessment.media_provenance` can describe it
with `media_provenance_cites` pointing to successfully read source text. This is explicitly
separate from an automated exact-image comparison. Without such citations, the reading says
no source-backed attribution is available, not that no provenance exists. OCR and visual
descriptions alone cannot establish its creator, date, location, or caption. Collected source
pages and unread search leads stay visible even when the model fails or times out.
Origin means the earliest observed copy in the sample, never proof
of first publication. Older clients remain compatible; missing claim checks default to `[]`.

## API

```
POST /api/lookup             {"url": "https://x.com/user/status/123"}   or {"text": "..."}
                             a text that is one Bluesky account (`@name.bsky.social`, `@name`,
                             `name.bsky.social`, `@example.com`, a DID) is looked up as its
                             profile link `https://bsky.app/profile/<handle>`; other text needs
                             5+ words (`signals.textnorm.word_count`: CJK/Thai counted per ~2 chars)
  -> 202 LookupResult (state "running"); repeated input within LOOKUP_CACHE_TTL_S -> cached result
GET  /api/lookup/{id}/events -> text/event-stream: `event: snapshot` + `data: <LookupResult>` at
                                once and on every change (checked every 150 ms), `event: ping`
                                every 10 s; ends after the snapshot whose state isn't "running"
GET  /api/lookup/{id}        -> LookupResult; the fallback: poll every ~0.6 s until state is
                                "done" or "failed"
POST /api/lookup/image       body: the raw image bytes (JPEG, PNG, WebP or GIF, at most 8 MB)
  -> 202 LookupResult with kind "image"; the same bytes within LOOKUP_CACHE_TTL_S -> cached result
GET  /api/lookup/{id}/image  -> the uploaded image, while the job lives (404 after that)
```

**Image lookups** (the meme / screenshot tracer). The type comes from the file's magic bytes,
not the declared content type. The bytes stay in the job, in memory, for `LOOKUP_JOB_TTL_S`
and are never written to disk or the database; the JSON carries only their sha256, type and
size. The upload is the submitted post (`upload:<sha256[:16]>`, no author, no time). Nemotron
reads it (its text goes on the card and into the monitor check and the reading), and:

- the results are matches of the picture, or posts and pages that carry at least 3 words in a
  row of its text (`textsim.caption_runs`; case and punctuation don't matter, a run of only
  stop words like "is in the" doesn't count).
- reverse-image search finds the picture: TinEye and Yandex find candidate pictures and optional
  local visual matching compares them.
- with 3+ words of text, the web, X and Bluesky searches look for it as an exact phrase, in
  reading order. A hit, and later the post read on its own page, joins the trace only with a
  3-word run, however few of its words it otherwise shares; the others are dropped. Each kept
  post or page lists its runs in `caption_match`, which Trackdown highlights in its text. The
  earliest copy read on its own page is `origin_id`.
- a site address written in the image (`textsim.domains`: `standwithjensen.com`, not big
  platforms' own like x.com, not file names) is searched for too, at most two: the web by the
  address, X and Bluesky by its name part when that is 8+ letters (`"standwithjensen"` also
  finds `#standwithjensen`), else by the address. A screenshot of a web page is found by its
  address far more often than by its first words, which are mostly the page's menus. A hit
  that names the site (the address, or its 8+ letter name part as a hashtag or under another
  ending: `#standwithjensen`, `standwithjensen.online`, never as a plain word or a file
  name like `analytics.js`) joins the trace
  like a 3-word run and is highlighted the same way.
- a copy of the image (a candidate for its origin), post or page, must repeat a real part of
  its text: runs covering 6+ words, or half of a shorter text; for a page, in what its own
  read returned, never the search snippet it was found by. A short post that only names the site or a few
  of its words is about the image, not a copy of it.
- Nemotron's description is never searched for, and nor is the looser coverage query (skipped).
  With fewer than 3 words of text and no site address, only the picture is searched for.
- an upload Nemotron types a screenshot of a post still goes through the screenshot step, which
  looks for the real post it shows.
- **same template, other content** (`lookup/threadvariants.py`, `media/templatematch.py`):
  people answer a screenshot with their own screenshot of the same page (a petition's signer
  search showing another name) or the same meme template with another caption. Those are other
  pictures, so reverse image search misses them, and X replies aren't indexed by search engines.
  The step reads up to 6 X threads through FxTwitter (an upload: the X posts its text and site
  searches found, the most replied-to first once their own reads are back, at most 5 s; an X
  post: its own thread, two reply pages), takes up to 24 reply pictures
  (not the traced picture itself), and Nemotron Super VL looks at each beside the traced
  picture, one call per picture, all at once (~0.5 s live): same site or app, and the same kind
  of screen content (a cropped result list counts; the site's confirmation page does not).
  Only the second decides. One picture per call, not a grid: in a grid the model judged each
  panel against the others and rejected real matches. A match is an `image_matches` entry with
  `relation: "template"` and `engine: "x_replies"`, with the reply's URL, its exact time
  (snowflake) and what differs; never the image origin, never a copy of the text. A picture
  the model finds unchanged is a copy, not another version: not counted. Matches are published
  as each comparison returns; the wait ends at the provider deadline (late ones are counted in
  the receipt). The lookup settles the scan before its reading, so the reading and the
  frozen result agree: a late answer or failure changes nothing, and a scan that hadn't
  started by then never starts. Reply pages that could not be read (a
  second page too) get a failed receipt; with none read, the step fails rather than reporting
  an empty scan.
- the upload is never its own origin: with no dated match, `origin_id` is null.

Uploads need a public URL: `LOOKUP_PUBLIC_BASE_URL`, or `https://{PUBLIC_HOST}`. The production
host is `noise-radar.srv2159172.hstgr.cloud`. Without one, the reverse-image step explains why
it is skipped. `LOOKUP_REVERSE_IMAGE=false` disables it (the API default is true). An uploaded
image's temporary public URL is sent to those search engines through Oxylabs; it still expires
with the lookup. For X/Bluesky, the first post image is used, not a link-card thumbnail.

Trackdown (web/trackdown) opens the stream and falls back to polling when it errors or no
snapshot arrives within 4 s (a buffering proxy). `deploy/Caddyfile` leaves the stream
uncompressed and unbuffered. The trace is finished on screen when the `assess` step is done;
the second reading is added to the panel when it arrives.

Errors: 400 bad input, 429 rate limit (`LOOKUP_MAX_PER_HOUR` overall, `LOOKUP_MAX_PER_IP_PER_HOUR`
per client), 503 when Oxylabs (or, for images, Nemotron) isn't configured, 404 unknown/expired
id, 413 image over 8 MB, 415 not a JPEG/PNG/WebP/GIF.
Jobs live in the API process (single uvicorn worker) for `LOOKUP_JOB_TTL_S`.

## LookupResult (JSON)

Results fill while `state` is `running`; the client re-renders on each poll. Times are ISO
8601 UTC. Unknown values are `null`, never guessed.

```jsonc
{
  "id": "lk_3f9a...",
  "state": "running",                 // running | done | failed
  "error": null,
  "input": {"url": "https://x.com/user/status/123", "text": null},  // image: {"image": {"sha256", "mime", "bytes"}}
  "kind": "x_post",                   // x_post | x_profile | bsky_post | bsky_profile | web | text | image
  "started_at": "...", "finished_at": null,
  "status": "insufficient",           // matched | insufficient  (see the status rule)
  "subject_id": "x:123",              // the submitted post's id in `posts`, or null
  "origin_id": "x:120",               // earliest known source: a post id, or a page id when an
                                      // earlier article carries the text (e.g. a quoted WSJ story); null if unknown
  "posts": [Post],
  "accounts": [Account],
  "pages": [Page],
  "media": [MediaFinding],
  "screenshots": [ScreenshotReceipt], // one per screenshot of a post in the submitted post's images
  "edges": [Edge],
  "clusters": [ClusterSummary],       // api/schemas.py ClusterSummary + "match": ["link"|"text"|"account"]
                                      // + "verdict_applies" (true only for link/text matches)
  "spread": Spread | null,
  "receipts": [Receipt],
  "assessment": Assessment | null,    // the primary reading (Nemotron's; Gemini's if Nemotron gave none)
  "readings": [Assessment],           // every reading, primary first; the second one can arrive while running
  "readings_disagree": false,         // the readings' llm_reading differ: both shown, neither is the status
  "steps": [Step],
  "notes": ["..."],
  "image_matches": [ImageMatch],
  "image_origin_id": "img:3" | null,  // separate from origin_id; see the image-origin rule below
  "swarm": SwarmReport | null,        // the swarm check (below); evidence only, never the status
  "swarm_live": {"id", "verdict", "n_accounts"} | null,  // the live Bluesky group of the same picture
  "swarm_sample": SwarmReport | null  // the search sample's report when `swarm` is the live one
}
```

### Swarm check

`lookup/swarm_step.py`, step `swarm` ("Swarm check"), after the posts are final and before the
reading; models in `swarm/model.py`. It runs for an upload and for an X or Bluesky post with a
picture (skipped otherwise: "no picture"). The posts that count are the submitted post (never the
upload's placeholder) and the copies, quotes, replies, Bluesky posts and origin the lookup kept
(an upload's only with a `caption_match`); never `mention`, `author_recent` or `screenshotted`.
For an X or Bluesky post lookup, a copy, origin or Bluesky post counts only with a picture of its
own (a text match without one did not post the picture); the submitted post always counts.
Each becomes a `Sighting` (quote/reply: `target` is the post it boosts; `time_exact` false for a
search-hint time; `media_key` the X media key or Bluesky blob CID). An X post read with a quoted
post lists the quoted post's pictures with its own, so none of its files count as its upload.
Account data comes from what the lookup read: profile reads (`accounts`), the author profile
FxTwitter sends with every post (exact join time, followers, following, posts, bio, avatar), and
one batched Bluesky `getProfiles` (at most 2 x 25, with a receipt, only when the provider time
allows). A month-only join date ("2009-06") is left out. The report has `basis: "sample"` (a
search sample: copies are missing).

The live Bluesky index is asked by the submitted Bluesky post's own blob CIDs, else by the
picture's perceptual hash (the upload, or the submitted post's own picture when the image reading
downloaded it; an image-reading cache hit downloads nothing, so then there is no picture match).
Never by a reply's or a text match's file: that may be another picture. The bytes are kept only
until the step ends. `swarm_live` is a small reference to the matched group (`{id, verdict,
n_accounts}`, its page `/?swarm=<id>`); `swarm` is the live group's report when it has more
posting accounts than the sample (the sample is then `swarm_sample`), else the sample's.

Time: the step ends by `min(now + 2 s, deadline - 10 s)`, so the primary reading's reserve stays
free, and is skipped ("no time left") with less than 0.3 s left. The sample and the live query run
side by side; the sample's report is published as soon as it is scored, and the live query (its
own 1 s limit) can only add to it. A late answer is dropped. After a live query that fails or
comes too late, the shared index (`swarm_step.LiveBackoff`) is left alone for 300 s, so a down
database costs one lookup's attempt. A failure leaves `swarm` null and changes nothing else. A
verdict describes the group of posts, never one account.

### Image matches

```jsonc
ImageMatch {
  "id": "img:3", "image_url": "https://example.org/picture.jpg",
  "thumb_url": "https://img.tineye.com/example.jpg" | null,
  "page_url": "https://example.org/story" | null, "domain": "example.org" | null,
  "engine": "tineye",               // tineye | yandex
  "first_seen": "2012-03-01T00:00:00Z" | null,
  "time_source": "crawl_date" | null, // crawl_date | page | snowflake | null, see below
  "time_uncertainty_s": 86400 | null, // true time in [first_seen, first_seen + this]; see below
  "time_receipt_id": 14 | null,      // the page read or post-id receipt behind first_seen
  "width": 388, "height": 257,       // either can be null
  "relation": "cropped",            // same | resized | cropped | edited | different | unknown
  "distance": 6,                     // null when not compared
  "crop_box": [0.05, 0.1, 0.95, 1.0] | null,   // fractions 0..1 of the upload, see below
  "edited_region": [0.0, 0.0, 1.0, 0.16] | null,
  "relation_note": "Visible image comparison",
  "changes": ["Caption changed from one line to another"],
  "change_kinds": ["caption_changed"],  // deduplicated, in Nemotron's order
  "changes_from": "match" | "upload" | null,  // which side the changes start from, see below
  "changes_withdrawn": false,  // true: a description was taken back (see below)
  "receipt_id": 12
}
```

`changes_from` says which way to read `changes` (media.imagediff reads one picture as the
starting point). `"match"`: the changes turn this copy into the submitted picture.
`"upload"`: they turn the submitted picture (the upload or the post's image) into this copy.
For an upload it is always `"match"`: a comparison convention, not a chronology (when someone
uploads a picture says nothing about when it was made or edited). For a post it is `"match"`
only when the copy's first-seen upper bound proves it existed before the post, judged on the
copy's latest dates; a description whose direction a late-arriving date flips is not shown,
or is withdrawn if already published (`changes`, `change_kinds` and `changes_from` cleared,
`changes_withdrawn: true`, `receipt_id` back on the comparison receipt; the description's own
receipt says why). With `changes_withdrawn` the empty `changes` say nothing about the content;
show "description withdrawn", not "no change".
Neither value says who copied whom or which picture was made first; show the direction with
the sentences (e.g. "This copy → your picture").

`change_kinds`: `caption_changed`, `text_added`, `text_removed`, `element_added`,
`element_removed`, `face_changed`, `cropped`, `recolored`, `watermark`, `other`.
Only `edited`/`cropped` matches get Nemotron change descriptions; code decides the relation.
`edited` comes from pixels alone (`media.variants`): a strong global brightness, contrast or
gamma filter on an unchanged repost can come back `edited`, so `edited` without `changes` is a
visual difference, not proof that the content changed.
`crop_box` and `edited_region` are `(left, top, right, bottom)` fractions 0..1 of the uploaded
image, the base of `media.variants.relate(upload, match)`, not pixels and not the match's own
frame: `crop_box` is where the match sits inside the upload (null when the match is larger),
`edited_region` is where it differs (draw it on the upload, or on the aligned match).
`different` matches are removed after comparison. Candidate IDs remain stable, so arrays can
shrink and IDs can have gaps. An unavailable comparison stays `unknown`, not an image origin.
Match dates. `first_seen` is the earliest upper bound known for the picture's first appearance,
from one of three sources: `crawl_date` (the day TinEye first saw a copy, not a publication
date; one day of slack), `snowflake` (the match's page or image URL is an X status: the post's
exact time from its id, `lookup.urls.snowflake_time`, no slack, receipt `x-id` /`snowflake`; an
id within a day of the snowflake epoch, i.e. a pre-snowflake sequential id, is not read as a
time), or `page` (the publication date printed on the matched page, read like a text lookup's
source page: Oxylabs `scrape_facts` + `service._page_time`, receipt `oxylabs` /`/scrape`).
`time_uncertainty_s` is `_page_time`'s uncertainty for a page date (86400 for a date only, 28 h
for a clock time without a zone, 0 with one); null means one day for `crawl_date` and exact
otherwise. A match keeps the source whose latest possible time is earliest. Page dates are read
for at most `Deps.image_page_dates_max` (3) matched pages per lookup, best match first (TinEye's
earliest, then undated ones), skipping X posts, bare image files and matches compared as a
different picture, each capped at 15 s (`imagetrace.PAGE_DATE_S`), started once TinEye has
answered. They run beside the downloads and comparisons and are never waited for (the trace's
end, which the family step waits on, does not move): a date counts if it lands before the family
step (lookup/family.py) reads its copies' dates, after its picture reads, or else before the
origin is chosen after post verification; a later one is dropped and its receipt stays "date not
confirmed", so the family tree and `image_origin_id` rest on the same dates. The family step
uses the same latest-possible-time rule (`FamilyMember.time_uncertainty_s`). Each page is read
once; its date applies to every match found on it (a match remembers every page it was found on,
not only its `page_url`), and a match takes the earliest of its pages' dates. `page_url` and
`domain` follow the date: they name the page (or the engine hit) whose date `first_seen` is. A
page that now redirects to another host (a repurposed domain, another blog on the same service,
even another subdomain: conservative) or to a front page (`/`, `/index.html`, `/home`, ...)
gives no date. A page date before 1991 or in the future is ignored. `image_origin_id` is the
same, resized or cropped match whose latest possible time is earliest and before every verified
post that may carry the picture.
`receipt_id` identifies the engine result until comparison, then the downloaded/comparison
receipt, or the Nemotron diff receipt; all earlier receipts remain in `receipts`.

Edges additionally accept `rel: "variant"` and tools `tineye`/`yandex`. A variant edge runs from
the image origin (else the earliest same/resized candidate, else the subject) to an edited or
cropped match; its tool is the engine or `nemotron` when it described changes. These edges show
visual relationships, not proof that one uploader copied another. Receipts additionally accept
tools `tineye`, `yandex`, and `fetch`: every engine request, download/redirect attempt and diff
call has a receipt, including failures and calls whose completion was not confirmed in time.

Two steps are inserted between `coverage` and `media`:
- `reverse_image`: **Web searched for the picture** (TinEye, Yandex).
- `variations`: **Copies compared**.

The trace starts before synchronous upload OCR, or just after the post read supplies its image
URL, alongside the existing searches. TinEye, Yandex imageview and Yandex sites run in parallel
(three Oxylabs `/scrape` calls; caps 20/15/15 s). Matches dedupe by image or thumbnail URL,
preferring TinEye and its earliest dates over promoted ranks; a duplicate fills in what the kept
match lacks (a page URL brings its own domain). At most `Deps.image_matches_max` (default 20)
matches are ever queued for download, counted cumulatively: a queued match is never evicted, so
a later hit never buys a second batch. Until TinEye answers, Yandex hits may take only the cap
minus TinEye's reserve (10, at most half the cap), since only TinEye's dated matches can be the
image origin; held-back Yandex hits get the free slots once TinEye is back. Downloads use eight
workers: 5 s and 8 MiB per attempt, full image then thumbnail (a thumbnail that arrives after
the full image failed is still tried, each URL once). Only HTTP(S) on ports 80/443, global
resolved IPs and `image/*` responses are accepted;
connections pin the checked IP, redirects are checked again, and no credentials/cookies are
sent. Up to `Deps.image_diffs_max` (default 4) edited/cropped comparisons run in parallel with
20 s Nemotron timeouts, starting only with 23 s remaining before the 10 s assessment reserve.
Every stage shares the lookup deadline; closed workers cannot publish late results.

`Deps.relate` / `Deps.describe_changes` can be injected. Otherwise they import
`media.variants.relate` / `media.imagediff.describe_changes` lazily. Without `media.variants`,
`variations` is skipped with `variation matching is not installed on this server` and reverse
results remain visible with relation `unknown`; without `media.imagediff` the comparisons still
run and only the change descriptions are missing (the step detail says so). Direct `Deps` callers default reverse search
off for backward compatibility; the API passes the enabled setting explicitly.

```jsonc
Post {
  "id": "x:2121381335956615658",      // "<platform>:<native id>"; bluesky uses the at:// uri
  "platform": "x",                    // x | bluesky | web
  "url": "https://x.com/lookner/status/2121381335956615658",
  "author_handle": "lookner",         // key into accounts (same platform)
  "author_name": "Steve Lookner",
  "text": "...",
  "created_at": "2026-11-13T23:14:58.508Z",
  "time_source": "snowflake",         // snowflake | api | page | search_hint
  "role": "submitted",                // submitted | origin | copy | quote | reply | mention | author_recent | bluesky | screenshotted
                                      // copy/quote/reply only after the post's own page was read
                                      // ("verified": true); a search hit is "mention" until then.
                                      // screenshotted: the real post a screenshot in the submitted
                                      // post shows; never a copy, never in the spread or the origin
  "similarity": 1.0,                  // token similarity to the submitted text, 0..1, null if n/a
  "metrics": {"views": 22300, "replies": 14, "reposts": 108, "likes": 205, "bookmarks": 21, "quotes": null},
  "metrics_as_of": "...",             // when the page was read
  "card": {"title": "...", "domain": "wsj.com", "url": "https://t.co/..."},   // or null
  "media_urls": ["https://pbs.twimg.com/media/...jpg"],
  "receipt_id": 3,
  "caption_match": ["when the comets land"] // uploaded image only: parts of `text` that repeat
                                      // 3+ words in a row of the image's text; else []
}
Account {
  "platform": "x", "handle": "lookner", "name": "Steve Lookner",
  "url": "https://x.com/lookner", "avatar_url": "...",
  "bio": "...", "location": "Boston, MA",
  "joined": "2009-06",                // year-month as shown on X; Bluesky: full date
  "age_days": 6000,                   // from joined to the subject post's time
  "followers": 252600, "following": 1284, "posts_count": 64800,
  "recent_posts_per_day": 41.2,       // from the recent post ids on the profile page, or null
  "receipt_id": 5
}
Page {
  "id": "page:1",                     // edges may point from/to page ids (rel "cites" / "link")
  "url": "...", "final_url": "...", "role": "linked",   // linked | coverage | reference
  "publisher": "...", "author": "...", "published_at": "...", "published_hint": "2026-11-13",
  "headline": "...", "claims": ["..."], "snippet": "...",
  "time": "2026-11-13T21:05:00Z",     // best known time (published_at, else search hint), or null
  "time_source": "page",              // page | search_hint | null
  "time_uncertainty_s": 86400,        // true time is in [time, time + this]; null if unknown
  "receipt_id": 7,
  "caption_match": []                 // as on Post, in `headline` or `snippet`
}
MediaFinding {
  "media_url": "...", "post_id": "x:...", "description": "...", "extracted_text": ["..."],
  "reuse_cues": ["..."], "manipulation_cues": ["..."], "confidence": 0.7,
  "model": "nemotron-3.5-super-vl", "latency_ms": 5400, "receipt_id": 9
}
ScreenshotReceipt {
  "media_url": "https://pbs.twimg.com/media/HXBBBB?format=jpg&name=large",  // the screenshot image
  "media_hash": "lookup:9f2c...",     // sha256 of the downloaded image bytes
  "post_id": "x:2121360054330986967", // the submitted post whose image is the screenshot
  // what Nemotron read in the screenshot (verbatim, nothing inferred)
  "platform": "x",                    // x | bluesky | threads | facebook | instagram | tiktok | other | unknown
  "handle": "ada_example",            // without @, or null if not legible
  "display_name": "Ada Example",
  "shown_time": "3:14 PM · Nov 13, 2026",   // as shown; relative ones ("2h") too
  "shown_text": "The comets have landed. That's the REAL reason for the ground stop.",
  "shown_counts": {"replies": 1800, "reposts": 5800, "likes": 35000, "views": null},
  "ui_oddities": ["the like count uses a font unlike the rest"],  // visible oddities, cautious
  "read_confidence": 0.82,
  "read_model": "nemotron-3.5-super-vl",
  // the check
  "label": "differs",                 // matches | differs | not_found | unchecked
  "reason": "the real post says \"a reason\", the screenshot \"the REAL reason\"",
  "original_id": "x:2121359000000000000",  // key into posts (role "screenshotted"), or null
  "original_url": "https://x.com/ada_example/status/2121359000000000000",
  "original_text": "The comets have landed. That's a reason for the ground stop.",
  "original_created_at": "2026-11-13T15:14:09.120Z",
  "original_time_source": "snowflake",     // snowflake | api | null
  "original_receipt_id": 14,               // the fetch that read the real post (its page/API)
  "similarity": 0.83,                 // screenshot text vs the real post's text, 0..1
  "diff": [                           // word runs; shot_only: only in the screenshot
    {"op": "same", "text": "The comets have landed. That's"},
    {"op": "shot_only", "text": "the REAL"}, {"op": "post_only", "text": "a"},
    {"op": "same", "text": "reason for the ground stop."}
  ],
  "differences": ["\"the REAL reason\" in the screenshot, \"a reason\" in the post"],
  "time_check": "consistent",         // consistent | inconsistent | unknown
  "shown_time_earliest": "2026-11-13T01:14:00Z",  // the real times the shown time allows
  "shown_time_latest": "2026-11-14T05:14:59Z",    // (time zone unknown: ±14 h); null = open
  "time_note": "shown time is in the viewer's unknown time zone (within 14 h of UTC)",
  "counts_now": {"views": 2900000, "replies": 1800, "reposts": 5800, "likes": 35000, "bookmarks": 2800, "quotes": null},
  "counts_note": "Counts change as people like and repost; shown as context, never as a verdict.",
  "candidates_checked": 3,            // real posts read and compared
  "receipt_ids": [9, 10, 11],         // the screenshot read, the searches, the posts read
  "latency_ms": 6100
}
Edge { "from": "x:120", "to": "x:123", "rel": "copy", "sim": 0.94, "tool": "oxylabs", "note": "same text" }
  // rel: repost | quote | copy | media | link | reply | cites | screenshot_of ; tool: oxylabs | nemotron | gemini | detector | bluesky
  // screenshot_of: from the post carrying the screenshot to the real post it shows; sim is the
  // screenshot-vs-post similarity, note the label (matches | differs)
Spread {
  "n_posts": 12, "n_accounts": 11, "n_verbatim": 9,       // verbatim = similarity >= 0.8
  "first_at": "...", "last_at": "...", "span_s": 4200,
  "first_hour_accounts": 7,                               // distinct accounts within 1 h of the first copy
  "min_gap_s": 12,                                        // smallest gap between verbatim copies by different accounts
  "note": "Search results are a sample of where the text appears, not every copy."
}
Receipt {
  "id": 3, "tool": "oxylabs",          // oxylabs | nemotron | gemini | bluesky | detector | x-id
  "endpoint": "/scrape",               // /scrape | /search | searchPosts | getPostThread | snowflake | db
  "summary": "X post page read: 14 replies, 108 reposts, 205 likes, 22.3K views",
  "source_url": "https://x.com/...",   // what was fetched or searched (search: the result page URL is in posts/pages)
  "query": null,                       // the search query, for /search
  "request_id": "1794612806305915961", "latency_ms": 9120, "cached": false, "at": "..."
                                       // cached: the same read in the last LOOKUP_PROVIDER_CACHE_TTL_S
                                       // (another lookup made it); latency 0, and the summary names
                                       // the original read's time, which X counts keep as metrics_as_of
}
Assessment {
  "model": "nemotron-3.5-super-vl",    // the model id the response returned (Gemini: its modelVersion;
                                       // the requested id, logged as such, only if the API returns none)
  "summary": "...",                    // 2-4 sentences
  "origin": "...", "corroboration": "...", "spread_pattern": "...",
  "counter_evidence": ["..."], "would_change": ["..."],
  "cites": [3, 7, 9],                  // receipt ids, all checked to exist
  "llm_reading": "organic_looking",    // organic_looking | copy_campaign_looking | unclear  (the model's read, not a verdict)
  "latency_ms": 1500, "cost_usd": null // null: no per-token price for the self-hosted endpoint; Gemini's is estimated
}
Step {
  "id": "post", "tool": "oxylabs", "label": "Read the X post",
  "state": "done",                     // pending | running | done | failed | skipped
  "detail": "14 replies · 108 reposts · 205 likes", "found": ["x:2121381335956615658"],
  "started_at": "...", "ended_at": "..."
}
```

Step ids: `input`, `post`, `author`, `linked`, `copies_web`, `copies_x`, `copies_bsky`,
`coverage`, `media`, `screenshot`, `verify`, `monitor`, `assess`, `second_reading`. Steps that don't apply are
`skipped`. Each step starts as soon as what it needs is known (`Lookup._phases`), not in phases:

1. `post` and `author` start together. The searches wait only for the post's text, not for the
   profile. A submitted web page gets a quick markdown read (its first heading, ~3 s) that starts
   the searches while `linked` (the AI fact extraction: publisher, date, claims, ~8-12 s) runs.
2. With the text: `copies_web`, `copies_x`, `copies_bsky`, `coverage`, the post's `linked` page
   and `media` (the images in parallel) all start at once. `screenshot` follows `media` on the
   same worker: each image Nemotron typed a screenshot is read as a post and checked against
   the real post (`lookup/receipts.py`); for an image-only post it runs alongside the searches.
   An uploaded image skips `coverage`; with fewer than 3 words of text in it, the other three
   searches too.
3. `verify` starts when `copies_x` and `copies_web` are back: their candidates, best match first
   (`LOOKUP_VERIFY_MAX` for the whole run), and the search-hit pages dated before the post. Each
   verified copy or quote queues its author's profile at once (`LOOKUP_PROFILES_MAX`). Each read
   of a candidate copy or older page gets `LOOKUP_VERIFY_READ_S` (8 s; Oxylabs `/scrape` p90 is
   ~7 s): the reading waits for the slowest of them, so a straggler stays an unread mention
   instead of holding everything back. The step then says "R of N read" (N queued, R came back);
   "more candidates not read" counts the ones never queued. Candidates
   from the search for an image's text are queued when `media` is done.
4. `monitor` runs once every search, page and image is in (it needs the link, the text and the
   Bluesky accounts), while `verify` finishes.
5. `assess` (Nemotron) and `second_reading` (Gemini) start together on the same receipts.

Every provider call checks the time budget first (`LOOKUP_TIME_BUDGET_S`, 60 s; the last 10 s
are kept for Nemotron's reading). The second reading has its own bound
(`LOOKUP_SECOND_READING_S`, 45 s) and never delays the first. A step that runs out says "time
budget used up". Same Oxylabs search or page read within `LOOKUP_PROVIDER_CACHE_TTL_S` (10 min)
and the same image with the same post text (by URL or bytes, 24 h) are served from a process
cache (`lookup/cache.py`), marked `cached` on their receipts with the original read's time
(search snippets' "3 hours ago" stays relative to that read). Images are downloaded and read
only while a bounded call (one attempt, 20 s, no rate-limiter wait: a lookup makes at most
two) still fits before the reading's reserve.

## Rules that keep it honest (and why)

- **Image origin is separate from text origin.** `image_origin_id` is the earliest dated
  `same`/`resized`/`cropped` match whose latest possible first-seen time precedes **every**
  dated verified post that may carry the picture (including posts discovered during
  verification): a post with an image that is the subject or a copy of it (role `submitted`,
  `origin`, `copy`, `quote` or `bluesky`), or any post whose image URL is one of the picture's
  own same/resized/cropped copies. Text-only copies, the author's other posts and replies say
  nothing about when this picture appeared, so they don't bound it. A crawl date is an
  upper bound on first appearance, not a publication date: the picture is at least that old
  and may be older. For day-only crawls, the conservative upper bound is the next midnight;
  a crawl on the same day as a post is not provably older. Without such posts, the
  earliest qualifying visual match can be the image origin. Without one, the field is null
  and a note explains why. It never changes `origin_id` or claims who created the picture.
  Describe differences as **edited** with the observed changes, never as a claim about intent.

- **Times.** X post times come from the post id (snowflake), so every X post found, even one
  only seen in a search result, has its exact creation time. Bluesky times are the record's
  `createdAt`. Web pages get the publisher's date when the scrape extracts one, otherwise the
  search engine's hint ("3 hours ago" relative to the search) marked `search_hint`.
- **Counts.** X hides zero counts on the logged-out page, and the reposts / likes / bookmarks
  numbers carry no label, so they are assigned only when all of them are shown; otherwise they
  are `null` (never guessed, never 0). Replies and the post's views are labelled and kept.
  Counts are what the page showed at `metrics_as_of`.
- **Copies are verified.** A search hit only says the text appears there. Up to
  `LOOKUP_VERIFY_MAX` candidate X posts are read on their own page: a quote of the post, a
  reply to it, or a copy of its words (similarity = share of the post's words the copy
  carries). On 13 Nov 2026 the top "copy" hit for a viral joke was in fact a quote post.
  X hits sharing under 35% of the words are dropped (search engines match loose keywords).
- **Images.** Nemotron reads the post's own images; their text (8+ words) is searched like the
  post text. Link-card thumbnails are described but not searched (their text is a photo label).
  A post that is only an image is searched by the text inside it.
- **Screenshots.** A screenshot of a post is a claim, checked against the real post: the label
  is `matches` (found, same words), `differs` (found, words or time differ; a word diff shows
  how), `not_found` ("no matching post found: it may be deleted, private or never posted", never
  "fake") or `unchecked` (with the reason: no legible handle, a platform we can't read, time
  budget used up). Counts in the screenshot are context only, never the label. A relative time
  ("2h") counts back from the post that carries the screenshot; an absolute time is in the
  viewer's unknown time zone, so it allows ±14 h. The label describes the screenshot, never a
  person. The real post is shown as `screenshotted`: never a copy, in the spread or the origin.
- **Short links** (t.co etc.) are resolved before the page is scraped.
- **Origin needs a clear lead.** A page is the origin only if the latest time its date allows
  (a day for a date-only hint, the unit for "3 hours ago") is before every post. Only posts read
  on their own page or API count as copies, in the spread and for the origin.
- **No verdict from a footprint.** `status` is `matched` only when a monitored cluster shared
  the link or the text. A cluster that only contains an account seen in the lookup is listed as
  context (`verdict_applies: false`), never as this link's verdict. Each model's `llm_reading`
  is shown as its reading, never as the status, and keeps only citations of receipts that
  exist; with no valid citation it is "unclear". Two readings that differ are both shown.
- **Model labels are what the server returned.** `Assessment.model` is the id in the response.
  Nemotron's reading, like its image reading, is kept only when that id names Nemotron 3.5
  Super VL (`media.nemotron.is_super_vl_model`); NVIDIA's shared build API once answered a
  Super VL request under a text-model label.

## Try it

```bash
uv run uvicorn api.main:app --port 8005          # needs OXYLABS_WEB_API_KEY; GEMINI/VERDA optional
curl -s -XPOST localhost:8005/api/lookup -H 'content-type: application/json' \
  -d '{"url":"https://x.com/StephenKing/status/2121360054330986967"}' | jq .id
curl -s localhost:8005/api/lookup/<id> | jq '.state, .steps[] | {id, state, detail}'
```

A typical X post lookup makes 8-20 Oxylabs calls (1 post page, 1-5 profiles, 4 searches, up
to 8 copy pages, 1 linked page), 1-3 Nemotron calls (images and the reading; one more per screenshot of a post) and 1 Gemini call.

## Timings (2026-11-14, live endpoints, seconds from the request)

Five real inputs (an X post, an X post with an image, an AP News Bluesky post, an AP article,
a sentence), 2 runs of the old phase order with Gemini's reading vs 3 runs of this one. Taken
from the steps' `started_at`/`ended_at`, the receipts' latencies and a 100 ms poll of the job
(what a client sees). Oxylabs latency varies a lot between runs (`/scrape` p50 3.6 s, p90 7.2 s).

| | Before | After |
|---|---|---|
| First facts on screen (post, author or page), median | 2.1 s | 0.4 s |
| Web page: searches start | 7-12 s (after the AI fact extraction) | ~4 s (quick markdown headline) |
| The reading call | 10-21 s (Gemini) | 1.2-2.0 s (Nemotron, reasoning off) |
| The reading on screen, median (range) | 32.5 s (23-41) | 14.6 s (4.5-22; one run with a hung Oxylabs read, before the per-call caps, 52) |
| Job done, median (Gemini's second reading included) | 32.5 s | 31 s |
| Trackdown: backend change to screen | 1.1-3.6 s (1.5 s poll, waited for `done`) | 0.03-0.16 s (SSE) |

Where the remaining time goes: Oxylabs. `verify` (candidate copy pages, then each copier's
profile; ~3-5 s each) is now the long pole at ~7-12 s, then ~1.5 s for Nemotron's reading.
Nemotron with reasoning on took 7.5-15 s per reading and returned nothing once (all 8192
tokens spent reasoning), so it stays off; with reasoning off, 20 of 20 readings were valid JSON
(guided decoding), cited only real receipts, and made the same call as Gemini on 3 of the 4
lookups (the AP article: Nemotron "unclear", Gemini "organic_looking"). Every number in their
text traced back to a receipt (rounded counts, spans computed from the times).
