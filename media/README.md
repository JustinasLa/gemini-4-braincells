# media/: Nemotron 3.5 Super VL media reader

For posts with images or video in **candidate clusters** (never the whole stream), Nemotron 3.5
Super VL describes the media, transcribes the text in it (OCR of memes, screenshots, ad
graphics) and lists cautious manipulated-context cues. Each result becomes an
`evidence(kind='media')` row that the API, the dashboard and the Gemini analyst read.

```
python -m media model                  # served model id + root, and the VL check
python -m media cluster 42 --dry-run   # read one cluster's media, print the findings
python -m media worker                 # compose service `media`: keep reading candidate clusters
python -m media.compare ...            # prompt/schema comparison (below)
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
5. **Store** (`worker.persist_evidence`): one row per (cluster, media hash), upserted so ids stay
   stable for analyst citations; unchanged rows aren't touched (no verdict churn). `model` is
   the model id the **response** returned. `source_url` is the first post on bsky.app;
   `detail` has `post_urls`, `profile_urls`, `dids`, `n_posts`, `n_accounts`, `media_url`,
   `media_kind`, `description`, `extracted_text`, `reuse_cues`, `manipulation_cues`,
   `content_flags`, `media_type`, `confidence`, `latency_ms`, `usage`, `prompt_version`.
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
- **VL check:** `describe_model()` reads `/models` at start-up, and every response's `model` must
  name a VL model (`is_vl_model`), else `NotVisionModelError` stops the worker (exit 2) before
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
- **Logging:** every call logs requested and returned model, latency, prompt, completion and
  reasoning tokens, and finish reason; the same numbers are stored in `detail.usage`.
- **Wording:** the prompt forbids guessing intent or calling anyone a bot/shill, and asks for
  "adult content" with no detail for sexual media (some spam clusters are porn ads).

## Prompt and schema iterations

Run on the same frozen sample of real candidate-cluster media (`python -m media.compare`;
the sample and per-item results stay outside git because they point at real posts).

Sample (2026-11-14 00:24 EET): 30 media items, the two most reused per cluster, from 18 live
candidate clusters (9 `coordinated`, 21 `insufficient` items; 13 images, 13 videos, 2 quote
posts with media); 27 were still downloadable. Endpoint: Verda, `nemotron-3.5-super-vl`.
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
