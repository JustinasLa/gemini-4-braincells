# media/: Nemotron 3.5 Super VL media reader

For posts with images or video in **candidate clusters** (never the whole stream), Nemotron 3.5
Super VL describes the media, transcribes the text in it (OCR of memes, screenshots, ad
graphics) and lists cautious manipulated-context cues. Each result becomes an
`evidence(kind='media')` row that the API, the dashboard and the Gemini analyst read.

## Where Nemotron is used

- **Candidate-cluster worker → evidence rows → dashboard + Gemini prompt.** Nemotron reads
  the images/video thumbnails; the dashboard shows its findings and provenance. Gemini receives
  each media evidence id, model id, description, bounded OCR (up to 600 characters), reuse and
  manipulation cues, account/post counts, and OCR-template shared/varying tokens and image counts.
  It can cite these rows like any other evidence; OCR is not proof of authorship or coordination.
- **Link lookup (`lookup/`, incoming on `dev/lookup`).** The lookup path sends the images of a
  submitted X/Bluesky post to Nemotron, then searches the OCR text to find where a screenshot
  circulates. This integration is being merged separately; it is not part of this worker change.

```
python -m media model                  # served model id + root, and the VL check
python -m media cluster 42 --dry-run   # read one cluster's media, print the findings
python -m media worker                 # compose service `media`: keep reading candidate clusters
python -m media.compare ...            # prompt/schema comparison (below)
python -m media.compare_screenshot ... # screenshot read, reasoning on vs off (below)
```

## Pipeline

1. **Candidates** (`store.candidate_clusters`): detector status in `MEDIA_STATUSES` (default
   `coordinated,insufficient`, in that priority) with `score >= MEDIA_MIN_SCORE` (0.5), and at
   least one live post with media. Most recently changed first; a cluster is re-read when
   signals updates it.
2. **Media** (`worker.group_media`): one group per blob CID (same CID = same file), most reused
   first (distinct accounts, then posts); at most `MEDIA_MAX_PER_CLUSTER` (4) per cluster.
3. **Fetch** (`blobs.BlobFetcher`, read-only): images from `cdn.bsky.app`, videos as their
   thumbnail from `video.bsky.app`, both by (did, CID); the author's PDS is an image fallback.
4. **Read** (`nemotron.NemotronClient`): one chat call per file, image sent as a data URL.
   Files typed `media_type=screenshot` get a second call, `read_screenshot` (`screenshot.py`,
   `screenshot-v1`, reasoning off by default: `MEDIA_SCREENSHOT_REASONING`,
   `MEDIA_SCREENSHOT_MAX_TOKENS`), that reads a screenshot of a social-media post as fields
   (platform, handle, shown time, text, counts, UI oddities) so lookup/ can find the original.
   Adult-flagged screenshots are never sent. A failed screenshot read keeps the main analysis,
   and the cluster is retried later (backoff), re-running only the screenshot read.
5. **Store** (`worker.persist_evidence`): one row per (cluster, media hash), upserted so ids stay
   stable for analyst citations; unchanged rows aren't touched (no verdict churn). Fetch/model failures are stored as retryable `detail.status=failed` rows (they are not used as cache hits), so the dashboard can distinguish failed media from media still pending. `model` is
   the model id the **response** returned. `source_url` is the first post on bsky.app;
   `detail` has `post_urls`, `profile_urls`, `dids`, `n_posts`, `n_accounts`, `media_url`,
   `media_kind`, `description`, `extracted_text`, `reuse_cues`, `manipulation_cues`,
   `content_flags`, `media_type`, `confidence`, `latency_ms`, `usage`, `prompt_version`.
   Screenshot rows also have `screenshot` (the `ScreenshotPost` fields, or null),
   `screenshot_status` (`post`, `not_post`, `failed`, `skipped_adult`),
   `screenshot_prompt_version` and, when failed, `screenshot_error`; a post adds one sentence to the summary ("Reads as a screenshot of an X
   post shown as from @handle."). `prompt_version` is `media-v5` (media-v3 plus the screenshot
   read in v4, plus the 20-entry `extracted_text` cap in v5), so older rows are no longer cache
   hits and each blob is read once more.
6. **OCR template** (`worker.ocr_template`): different files whose in-image text is mostly the
   same (token Jaccard >= 0.5) across >= 2 accounts get one extra row,
   `detail.type='ocr_template'`, `detail.media_hash='ocr-template'`, listing the words that
   change. Example: four "LAW FIRMS USE VIDEO MARKETING" ads from four accounts that differ only
   in the city name. A blob hash can't see that; only the OCR can.

Identical files across accounts are already a detector signal (`duplicate_media` in signals/).

## Model, and the checks around it

- Endpoint: the dedicated Verda vLLM deployment when `VERDA_INFERENCE_KEY` is set
  (`VERDA_BASE_URL`, `VERDA_MODEL=nemotron-3.5-super-vl`, root
  `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4`); otherwise NVIDIA's build API
  (`NVIDIA_API_KEY`, `NVIDIA_MEDIA_MODEL`).
- **Super VL check:** `describe_model()` reads `/models` at start-up, and every response's `model`
  must contain the Nemotron, Super and VL tokens (`is_super_vl_model`), else
  `NotVisionModelError` stops the worker (exit 2) before
  anything is stored. Reason: on 2026-11-14 NVIDIA's build API answered an image request for
  `nvidia/nemotron-3.5-super-vl-120b-a12b` under the label
  `private/nvidia/nemotron-3.5-super-120b-a12b`, so we can't claim Super VL from there.
- **Reasoning:** on by default. Its tokens count against `max_tokens`: with v1's 700 tokens the
  reply was often cut off with `content` null. `MEDIA_MAX_TOKENS=4096`;
  `MEDIA_REASONING=false` turns it off through `chat_template_kwargs.enable_thinking`.
  Parsing takes the first JSON object and tolerates code fences, `<think>` blocks and prose;
  fields are normalised (string to list, unknown flags dropped, confidence clamped).
- **Cost control:** cache by media hash (in process, plus any stored row with the same CID and
  `prompt_version`, from any cluster), rate limit `MEDIA_RPM` (30), exponential backoff with
  jitter on 429/5xx/connection errors (4 tries), and failed blobs are retried with backoff
  (5 min doubling to 6 h), not every pass.
- **Logging:** every API attempt, including failures and comparison runs, logs requested and
  returned model, latency in ms, prompt/completion/reasoning tokens, finish reason, attempt number
  and outcome (`ok`, `retry`, or an error type). Missing response fields are `None`, not zero.
  Successful analyses also store tokens in `detail.usage` and latency in `detail.latency_ms`.
- **Wording:** the prompt forbids guessing intent or calling anyone a bot/shill, and asks for
  "adult content" with no detail for sexual media (some spam clusters are porn ads).

## Prompt and schema iterations

Run on the same frozen sample of real candidate-cluster media (`python -m media.compare`;
the sample and per-item results stay outside git because they point at real posts).

Sample (2026-11-14 00:24 EET): 30 media items, the two most reused per cluster, from 18 live
candidate clusters (9 `coordinated`, 21 `insufficient` items); 27 were still downloadable.
The previously recorded type tally (13 images + 13 videos + 2 quote-media items) totals only 28,
not 30, so it is not a complete breakdown. `compare.py` samples by unique blob, not fixed type
quotas; the frozen sample is not retained in this repository, so the remaining types cannot be
verified here. Endpoint: Verda, `nemotron-3.5-super-vl`.
"Explicit words" uses a word list in `compare.py` that is broader than the guard's.

| Version | Change | Valid JSON | `content` null | Cut off at `max_tokens` | OCR text found | Explicit words in description | Adult items flagged | p50 latency | Mean completion (reasoning) tokens |
|---|---|---|---|---|---|---|---|---|---|
| v1 (valtteri/nemotron) | forensic prompt, JSON mode, `max_tokens=700`, reasoning on by default | 4/27 | 18 | 23 | 1 | 1 | n/a | 7.0 s | 672 (640) |
| v2 | public-evidence rules, fixed `media_type` / `content_flags` vocab, "describe media not people", `max_tokens=4096`, tolerant parser | 27/27 | 0 | 0 | 18 | 4 | 10 | 8.7 s | 1370 (1199) |
| v2, reasoning off | same prompt, `enable_thinking=false` | 26/27 | 0 | 1 (looped to 4096) | 17 | 3 | 8 | 3.1 s | 324 (0) |
| **v3 (current, `media-v3`)** | adult media: description must be exactly "Adult content."; plus a code guard that replaces any flagged or explicit description | 27/27 | 0 | 0 | 18 | 0 | 10 | 2.9 s | 1164 (1011) |

What we learned:
- v1 failed on budget, not on the prompt: reasoning used about 640 of its 700 tokens, so 23 of
  27 replies were cut off and 18 had no `content` at all. Raising the budget fixed it.
- Reasoning on beats reasoning off for robustness (one reasoning-off reply looped until the
  budget ran out) and finds slightly more text, so it stays on. Latency differs between runs
  on the shared endpoint (v2 vs v3 are about 1000 reasoning tokens each, 8.7 s vs 2.9 s p50), so
  read latency as a rough guide.
- The model flags adult media reliably (10 of the 10 adult items in v2 and v3) but still
  described bodies in 4 of them under v2; v3's rule plus the guard bring that to 0. The guard
  stays as a backstop because a prompt is not a guarantee.
- No version called a person a bot, shill, spammer or troll (0 in every row).
- v5 (`media-v5`, 2026-11-14): a page of text (an image of 100+ lines) made v4 transcribe
  every line, about 2000-3000 completion tokens, while the client keeps only the first 20. With
  reasoning on that ran into `max_tokens=4096` (2 of 3 reads of a 172-line test image failed
  with an unterminated string; the link lookup showed "Nemotron returned invalid analysis").
  v5 asks for at most 20 entries of at most 300 characters: 7 of 7 reads of the same two test
  images stopped normally, at 1270-2440 completion tokens. If a reply is still cut
  (`finish_reason=length`), `parse_content(..., truncated=True)` keeps the fields the model
  finished, so that read is kept with `finish_reason="length"` instead of being lost. A reply cut
  before its description is still an error.

## Screenshot read (`screenshot-v1`)

`NemotronClient.read_screenshot(MediaInput) -> ScreenshotPost | None` (`media/screenshot.py`)
reads an image the main read typed `screenshot` as a social-media post: `platform` (from the UI
look), `display_name`, `handle` (no `@`), `shown_time` (verbatim), `text` (the post's own text,
verbatim), `counts` (replies, reposts, quotes, likes, views, bookmarks; `"2.5K"` -> 2500,
`None` = not shown, never a guessed 0), cautious `ui_oddities`, `confidence`, `model`,
`latency_ms`, `prompt_version`. None when the image is not a post (chat, article, chart, app
screen) or nothing findable (handle, name, text) is legible. lookup/ uses it to find the real
post and compare the screenshot against it. The normaliser maps platform and count aliases,
drops UI-oddity notes that describe people (bot, shill...) or judge a date against the model's
own knowledge ("in the future").

Measured with `python -m media.compare_screenshot` on real screenshots from candidate clusters
(the sample and results stay outside git: they point at real posts). The sampler leaves out
adult-flagged media, and `run` skips any it is given, as the worker does. Samples: **A**, the 40
newest distinct `media-v3` screenshots (21 clusters); **B** (`--likely-posts`), 34 screenshots
whose OCR has an @handle or whose description mentions a post (19 clusters, 19 also in A).
Endpoint: Verda, `nemotron-3.5-super-vl` (root
`nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4`), 4 parallel calls, 2026-11-14 07:25 UTC,
final prompt. "Legible" counts are among items read as posts; grounding = share of the read
text's words that the media-v3 OCR also found (a hallucination check).

| Sample | Reasoning | Valid JSON | Cut off | Posts | Handle legible | Text legible | Text grounding (median, <0.5) | p50 latency | p90 latency | Mean completion (reasoning) tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| A (40) | on, `max_tokens=4096` | 40/40 | 0 | 1 | 1/1 | 1/1 | 1.0, 0 | 1.67 s | 2.23 s | 720 (607) |
| A (40) | **off**, `max_tokens=1536` | 40/40 | 0 | 1 | 1/1 | 1/1 | 1.0, 0 | 0.27 s | 0.35 s | 112 (0) |
| B (34) | on, `max_tokens=4096` | 34/34 | 0 | 7 | 7/7 | 6/7 | 1.0, 0 | 1.66 s | 3.85 s | 855 (731) |
| B (34) | **off**, `max_tokens=1536` | 34/34 | 0 | 7 | 7/7 | 6/7 | 1.0, 0 | 0.26 s | 0.41 s | 121 (0) |

Agreement, on vs off: same is_post on 74/74, same handle on 7/8 posts, text Jaccard median 1.0.
Two reasoning-off runs agreed on every field (off #2: p50 0.24-0.25 s). Earlier runs on two
similar samples gave the same picture.

What we learned:
- **Reasoning stays off for this read** (`MEDIA_SCREENSHOT_REASONING=false`): 6x faster, a
  much tighter worst case, no format or cut-off failures, and the same posts and text. Unlike
  the main read (v2 reasoning off looped once), this output is short (about 120 tokens), and
  the 1536 budget caps a loop.
- Where on and off differed, reasoning on was wrong: it "corrected" a shown handle to the
  account's web domain (off read it as shown), and in an earlier run invented a repost count
  and a time on a non-English post and flagged "the date is in the future" (the model judges
  dates against its training cutoff). The prompt now forbids date judgements and the
  normaliser drops them.
- Most prod "screenshots" are not posts: charts, radar maps, flight trackers, headlines, court
  filings, TV grabs. is_post was right on every image checked by eye (74, earlier samples): no
  false posts, no missed posts. Only 8 post reads are in the samples (7 in B, 1 in A), so
  field accuracy rests on a handful of posts; re-measure as more reach prod.
- Known limit: with reasoning off, a Truth Social post still reads as `x` (reasoning on says
  `other`, after the "not in the list is other, never x" rule). On a cut-off Bluesky post the
  model read the next, fully legible post of the thread (both settings).
- No count was ever written as 0 when not shown; no UI-oddity note described a person.

## Image diff (`imagediff-v1`)

`describe_changes(client, original, variant, *, timeout_s, edited_region=None) -> ImageDiff | None`
(`media/imagediff.py`) says in plain sentences what changed between an earlier meme and a later
variant, for the meme trace: `changes` (at most 5), `kinds` (one per change, from
`caption_changed`, `text_added`, `text_removed`, `element_added`, `element_removed`,
`face_changed`, `cropped`, `recolored`, `watermark`, `other`), `text_before` / `text_after`
(legible text in each), `confidence`, `model` (as the response returned it), `latency_ms`. No
visible change gives `changes == []`. Inputs: JPEG (incl. MPO), PNG, GIF, WebP; formats that
decode inside `Image.open` (ICO) are refused. It returns None, never raises, on bad image bytes,
another format, an image over 24 MP (checked from the header, before decoding), timeout, an API error, a non-Super-VL response, a reply cut at `max_tokens` (a cut
could lose an unfinished `content_flags`) or an unusable reply. A non-Super-VL response, which
stops the worker, is logged at ERROR here but still returns None: the lookup must not break.

- **One call per pair**, reasoning off, guided `json_schema` (as in `lookup/assess.py`).
  `timeout_s` counts from entry: image decoding, then the rate-limiter wait, then the request,
  which gets what is left as its HTTP timeout. No request starts with less than 0.5 s left after
  the limiter's wait (None instead). The HTTP timeout applies per read, so a reply that completes
  after the deadline is dropped (None). A bare 5xx gets one retry, with its own limiter slot, if
  at least 3 s of the budget is left. Each request is logged by `log_call_attempt` with
  `task=imagediff`, and the Super VL check applies. It calls the OpenAI client directly, not `NemotronClient._complete`, whose 4-try
  backoff cannot keep to a lookup's budget.
- **Layout:** Verda accepts two images per request, so both were measured. The default is one
  composite image: both scaled to one height, labelled "A: earlier" / "B: later", at most 2048 px
  wide. `edited_region` (fractions of the variant) draws a red box on B, after the composite
  is scaled so the line stays visible. Only then is the model told about the box, and a "red
  box/outline/border/frame" sentence is dropped as our own marker.
- **Wording:** describes the media, never names a person. A sentence with deepfake, fake, real,
  authentic, genuine, bot, shill, troll or propaganda in our own words is dropped with its kind.
  A quoted span is exempt only when the reply's own transcription confirms it is image text (a
  whole transcribed line, or 2+ consecutive transcribed words), so a caption swap from 'REAL
  NEWS' to 'FAKE NEWS' is reported, while scare quotes or a quoted 'red box' are not exempt. Adult or graphic
  media (model flag or the shared `safe_description` word list) gives exactly "Adult content." or
  "Graphic content." with no text lists.

Evaluated on 20 pairs made from 7 public-domain Wikimedia Commons photos (the images and pairs
stay outside git). Pairs: 3 caption swaps, 1 caption swap plus watermark, text added, text removed,
an element added, 2 elements removed, 2 face patches, 2 crops, a hue shift, a grayscale, a
watermark added, a watermark removed, and 3 identical pairs (re-encoded at JPEG q55-70 and/or
resized to 80-90%). Endpoint: Verda, `nemotron-3.5-super-vl`, sequential calls, 2026-11-14,
final prompt, 3 runs. "Kind" = the expected kind is among `kinds`; "clean" = additionally no
unrelated kind (a crop may also report what it cut off), and an identical pair returns no change.

| Layout | Valid | Kind right | Clean | Caption text read | p50 latency | p90 latency |
|---|---|---|---|---|---|---|
| **composite (default)** | 60/60 | 57/60 | 57/60 | 12/12 | 0.24-0.30 s | 0.37-0.40 s |
| pair (two images) | 60/60 | 53/60 | 55/60 | 12/12 | 0.27-0.38 s | 0.37-0.51 s |
| composite + `edited_region` box (2 runs) | 40/40 | 37/40 | 34/40 | 8/8 | 0.29-0.36 s | 0.38-0.43 s |

Per kind, composite, each of 3 runs: caption changed 4/4, text added 1/1, text removed 1/1,
element added 1/1, element removed 1/2, face changed 2/2, cropped 2/2, recolored 2/2, watermark
2/2, identical 3/3. No reply had a forbidden word, a person's name, panel words or our red box.

What we learned:
- vLLM's guided decoding rejects `uniqueItems` (HTTP 400 "Unimplemented keys" on every call), so
  the schema leaves it out.
- Verda answers some calls with a bare HTTP 500, whatever the request shape (schema,
  `json_object`, none): about 70% in one busy period, 0-10% otherwise. Hence the one retry.
- Pair layout reported a crop that is not there in an identical pair (a re-encoded portrait) in 3
  of 3 runs (an earlier prompt also gave "warmer tone"). Composite returned no change for all 3
  identical pairs in every run.
- With a red `edited_region` box, the first prompt reported the box as an added element (7 of the
  12 valid pair-layout replies). The prompt rule and the code guard brought that to 0, but the box did not
  improve accuracy on these pairs. Composite without a box was the most accurate.
- An early composite prompt wrote "in the later image" / "left panel". Banning panel words fixed
  that, but direction then needs its own rule ("in A but missing from B was removed").
- Known limit: a colour sunflower inset pasted on a grayscale portrait, then removed, reads as
  "added" in composite in every run. Removed sunglasses and a removed watermark read correctly.
  Pair layout gets the inset right, but it is less accurate overall.
- Latency is far below the 4 s target: the reply is about 100 tokens with reasoning off.
