# lookup/: trace one link through X, Bluesky and the web

A user pastes a link (an X post, a Bluesky post, a profile or any web page). The lookup collects
real, live data about it and returns it with a receipt for every fact:

- **The post itself** (Oxylabs `/scrape` of the X page; Bluesky public AppView): author, text,
  exact creation time, the counts X shows (views, replies, reposts, likes, bookmarks), the
  linked card and the media.
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
- **Our monitor**: whether the link, text or accounts are in a cluster the detector watches
  (`api/store.py`), and that cluster's detector status.
- **Gemini** writes a short, cited reading of the collected receipts: origin, corroboration,
  spread pattern, counter-evidence, what would change the reading. It may cite only receipt ids
  that exist (checked in code). It never sets the status.

**Status rule (honesty):** `status` is `matched` only when the input hits a cluster we monitor;
the coordination verdict is then that cluster's detector status. Otherwise `status` is
`insufficient`: one link's web footprint is not proof of coordination, and the copy pattern is
shown as data (counts, times, accounts), never as a verdict. Search results are a sample, not a
census: the notes say so.

## API

```
POST /api/lookup        {"url": "https://x.com/user/status/123"}   or {"text": "..."}
  -> 202 LookupResult (state "running"); repeated input within LOOKUP_CACHE_TTL_S -> cached result
GET  /api/lookup/{id}   -> LookupResult; poll every ~1.5 s until state is "done" or "failed"
```

Errors: 400 bad input, 429 rate limit (`LOOKUP_MAX_PER_HOUR` overall, `LOOKUP_MAX_PER_IP_PER_HOUR`
per client), 503 when Oxylabs isn't configured, 404 unknown/expired id.
Jobs live in the API process (single uvicorn worker) for `LOOKUP_JOB_TTL_S`.

## LookupResult (JSON)

All arrays grow while `state` is `running`; the client re-renders on each poll. Times are ISO
8601 UTC. Unknown values are `null`, never guessed.

```jsonc
{
  "id": "lk_3f9a...",
  "state": "running",                 // running | done | failed
  "error": null,
  "input": {"url": "https://x.com/user/status/123", "text": null},
  "kind": "x_post",                   // x_post | x_profile | bsky_post | bsky_profile | web | text
  "started_at": "...", "finished_at": null,
  "status": "insufficient",           // matched | insufficient  (see the status rule)
  "subject_id": "x:123",              // the submitted post's id in `posts`, or null
  "origin_id": "x:120",               // earliest known source: a post id, or a page id when an
                                      // earlier article carries the text (e.g. a quoted WSJ story); null if unknown
  "posts": [Post],
  "accounts": [Account],
  "pages": [Page],
  "media": [MediaFinding],
  "edges": [Edge],
  "clusters": [ClusterSummary],       // api/schemas.py ClusterSummary, matched monitored clusters
  "spread": Spread | null,
  "receipts": [Receipt],
  "assessment": Assessment | null,
  "steps": [Step],
  "notes": ["..."]
}
```

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
  "role": "submitted",                // submitted | origin | copy | quote | reply | mention | author_recent | bluesky
  "similarity": 1.0,                  // token similarity to the submitted text, 0..1, null if n/a
  "metrics": {"views": 22300, "replies": 14, "reposts": 108, "likes": 205, "bookmarks": 21, "quotes": null},
  "metrics_as_of": "...",             // when the page was read
  "card": {"title": "...", "domain": "wsj.com", "url": "https://t.co/..."},   // or null
  "media_urls": ["https://pbs.twimg.com/media/...jpg"],
  "receipt_id": 3
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
  "receipt_id": 7
}
MediaFinding {
  "media_url": "...", "post_id": "x:...", "description": "...", "extracted_text": ["..."],
  "reuse_cues": ["..."], "manipulation_cues": ["..."], "confidence": 0.7,
  "model": "nemotron-3.5-super-vl", "latency_ms": 5400, "receipt_id": 9
}
Edge { "from": "x:120", "to": "x:123", "rel": "copy", "sim": 0.94, "tool": "oxylabs", "note": "same text" }
  // rel: repost | quote | copy | media | link | reply | cites ; tool: oxylabs | nemotron | gemini | detector | bluesky
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
}
Assessment {
  "model": "gemini-4-hydrogen",
  "summary": "...",                    // 2-4 sentences
  "origin": "...", "corroboration": "...", "spread_pattern": "...",
  "counter_evidence": ["..."], "would_change": ["..."],
  "cites": [3, 7, 9],                  // receipt ids, all checked to exist
  "llm_reading": "organic_looking",    // organic_looking | copy_campaign_looking | unclear  (Gemini's read, not a verdict)
  "latency_ms": 14000, "cost_usd": 0.03
}
Step {
  "id": "post", "tool": "oxylabs", "label": "Read the X post",
  "state": "done",                     // pending | running | done | failed | skipped
  "detail": "14 replies · 108 reposts · 205 likes", "found": ["x:2121381335956615658"],
  "started_at": "...", "ended_at": "..."
}
```

Step ids in order: `input`, `post`, `author`, `linked`, `copies_web`, `copies_x`,
`copies_bsky`, `coverage`, `media`, `monitor`, `assess`. Steps that don't apply are `skipped`.

## Rules that keep it honest (and why)

- **Times.** X post times come from the post id (snowflake), so every X post found, even one
  only seen in a search result, has its exact creation time. Bluesky times are the record's
  `createdAt`. Web pages get the publisher's date when the scrape extracts one, otherwise the
  search engine's hint ("3 hours ago" relative to the search) marked `search_hint`.
- **Counts.** X hides zero counts on the logged-out page, so a missing number is `null`, never
  0. Counts are what the page showed at `metrics_as_of`.
- **Copies are verified.** A search hit only says the text appears there. Up to
  `LOOKUP_VERIFY_MAX` candidate X posts are read on their own page: a quote of the post, a
  reply to it, or a copy of its words (similarity = share of the post's words the copy
  carries). On 13 Nov 2026 the top "copy" hit for a viral joke was in fact a quote post.
  X hits sharing under 35% of the words are dropped (search engines match loose keywords).
- **Images.** Nemotron reads the post's own images; their text (8+ words) is searched like the
  post text. Link-card thumbnails are described but not searched (their text is a photo label).
  A post that is only an image is searched by the text inside it.
- **Short links** (t.co etc.) are resolved before the page is scraped.
- **No verdict from a footprint.** `status` is `matched` only when the monitor's database has a
  cluster with the link, text or Bluesky accounts. Gemini's `llm_reading` is shown as its
  reading, never as the status, and keeps only citations of receipts that exist.

## Try it

```bash
uv run uvicorn api.main:app --port 8005          # needs OXYLABS_WEB_API_KEY; GEMINI/VERDA optional
curl -s -XPOST localhost:8005/api/lookup -H 'content-type: application/json' \
  -d '{"url":"https://x.com/StephenKing/status/2121360054330986967"}' | jq .id
curl -s localhost:8005/api/lookup/<id> | jq '.state, .steps[] | {id, state, detail}'
```

A typical X post lookup makes 8-20 Oxylabs calls (1 post page, 1-5 profiles, 4 searches, up
to 8 copy pages, 1 linked page), 0-2 Nemotron calls and 1 Gemini call, in 20-40 s.
