# Architecture

How the services fit together, what each directory does, and the contracts they share.
The detection method itself is in [METHODOLOGY.md](METHODOLOGY.md).

## Overview

```
   Bluesky Jetstream (public firehose, read-only)
              │ posts, reposts, likes, follows, account events
              ▼
         ingest/ ──────────────▶ Postgres + pgvector ◀────────── eval/
                                   ▲        │                 (recorded slices → eval_runs:
                                   │        │                  recall, false-positive rate)
                                   │        ▼
                          signals/  every minute (10 min window) and every 5 min (1 h window):
                                    shared keys → account graph → clusters →
                                    4 signal families + null-model p-value + organic guards →
                                    status (coordinated | likely_organic | insufficient)
                                   │
       ┌───────────────────────────┼───────────────────────────┐
       ▼                           ▼                           ▼
   sources/                     media/                      analyst/
   Oxylabs Web API              Nemotron 3.5 Super VL       Gemini
   /scrape linked pages,        reads images / frames:      explains the status, cites
   /search where the text       OCR, reuse cues             evidence ids, counter-evidence;
   or headline appears                                      code-checked (analyst/guard.py)
       └───────────────────────────┴───────────────────────────┘
                                   ▼  evidence (signal | source | media | analyst)
                         api/ (FastAPI) ──▶ web/ dashboard   (Caddy, behind Hostinger's Traefik)
```

## Components

| Dir | What it does |
|---|---|
| `ingest/` | Bluesky Jetstream consumer → Postgres (`posts`, `reposts`, `likes`, `follows`, `account_events`); hour-partitioned raw tables with retention; record/replay. See `ingest/README.md`. |
| `signals/` | The coordination detector: windows, shared keys, account graph, clusters, four signal families, permutation null model, organic guards, deterministic status, enrichment. See `signals/README.md`. |
| `sources/` | Oxylabs Web API: `/scrape` with AI extraction on a cluster's most-shared links, `/search` for its headline and repeated text. Every fact keeps its source URL. |
| `media/` | Nemotron 3.5 Super VL: describes images and video thumbnails in candidate clusters, extracts their text (OCR), notes reuse and manipulated-context cues, links different images that share one text template. `media/README.md`. |
| `lookup/` | Trace any link (X post, Bluesky post, profile, web page) with live data: Oxylabs `/scrape` reads the post, its counts and the author's profile; `/search` finds the same text on the web, on X and Bluesky and the coverage of the story; candidate copies are verified on their own pages (copy, quote or reply); exact X post times from the post id; Nemotron reads the images and writes the cited reading in about 1.5 s; Gemini adds a slower second reading beside it. Steps run as soon as their inputs are known and stream to the page (server-sent events). Coordination verdict only when the link is in a monitored cluster. `lookup/README.md`. |
| `analyst/` | Gemini: per-cluster verdict JSON with cited evidence, counter-evidence and what would change it; guarded by code. |
| `eval/` | Recall on injected synthetic campaigns, false-positive rate on organic bursts, on held-out hours → `eval_runs`. |
| `api/` | FastAPI: clusters, cluster detail, metrics, health, ad-hoc `POST /api/analyze`. |
| `web/` | Trackdown, the web UI, served by Caddy at `/`. |
| `common/` | Shared settings (`common.config`) and DB connections (`common.db`), migrations runner. |
| `db/` | `schema.sql` plus `migrations/`. |
| `deploy/` | Docker Compose, Caddy, deploy and outside-check scripts for the Hostinger VPS. |

## Tech stack

| Part | Where it is used |
|---|---|
| **Bluesky Jetstream** (public firehose) | Data source. `ingest/consumer.py` reads it over WebSocket; `ingest/writer.py` stores it. |
| **Bluesky AppView + PLC directory** | Account enrichment (profile, creation date, handle history) in `signals/enrich.py`. |
| **Oxylabs Web API** | `sources/oxylabs.py` (`/scrape` with AI extraction, `/search`), called by the `sources` worker and by `POST /api/analyze` (`api/routes/analyze.py`). Gemini also calls it as a tool (`analyst/tools.py`). |
| **Google Gemini** (`google-genai`) | `analyst/agent.py` (function-calling loop over Oxylabs and DB tools) and `analyst/gemini.py`; checked by `analyst/guard.py`. The second reading of a link lookup (`lookup/assess.py`). Embeddings for the semantic signal (`signals/semantic.py`) and the red-team fixtures (`signals/attack.py`). |
| **NVIDIA Nemotron 3.5 Super VL** | Self-hosted on Verda (vLLM, OpenAI-compatible; `openai` client). Reads images and video frames in candidate clusters (`media/`), reads a looked-up post's images, and writes the link lookup's primary cited reading (`lookup/assess.py`: text-only chat, reasoning off, guided JSON, ~1.5 s). Every response must name the Super VL model. |
| **Python 3.12 + uv** | Every backend service. `pyproject.toml` / `uv.lock`; one image in `Dockerfile`. |
| **PostgreSQL 16 + pgvector** | The only datastore: `db/schema.sql`, `db/migrations/`, applied by `common/migrate.py`. Raw tables are hour-partitioned. pgvector is enabled but no vector columns exist yet. |
| **psycopg 3** | All DB access: `common/db.py`, `ingest/`, `signals/store.py`, `sources/store.py`, `analyst/store.py`, `api/store.py`. |
| **datasketch** (MinHash LSH) | Near-duplicate text keys in `signals/keys.py`. |
| **NumPy + SciPy (sparse)** | Account co-action graph in `signals/graph.py`; permutation null model in `signals/score.py`. |
| **python-igraph + leidenalg** | Splitting large connected components into clusters (Leiden) in `signals/graph.py`. |
| **httpx** | Outbound HTTP: `sources/oxylabs.py`, `signals/enrich.py`. |
| **Pydantic + pydantic-settings** | Settings from env (`common/config.py`); API models (`api/schemas.py`), verdicts (`analyst/verdict.py`), source facts (`sources/models.py`). |
| **FastAPI + Uvicorn** | The JSON API: `api/main.py`, `api/routes/`. |
| **Caddy 2** | Serves `web/public` and proxies `/api/*` to the API (`deploy/Caddyfile`). |
| **Docker Compose** | All services (db, migrate, ingest, signals, sources, analyst, media, api, caddy): `deploy/docker-compose.yml`. |
| **Hostinger VPS + Traefik** | Production host. Hostinger's Traefik terminates HTTPS in front of our Caddy (`deploy/docker-compose.hostinger.yml`); shipped with `deploy/deploy.sh`, checked with `deploy/check.sh`. |
| **pytest + ruff** | Tests in `tests/`; lint and format config in `pyproject.toml`. |
| **Entire** (Graph, Brain) | Agent session checkpoints and code lookup for the team (see [DEVELOPMENT.md](DEVELOPMENT.md#entire-agent-sessions-graph-brain)). |

## Shared interfaces

Change these only with a note in this section, so the other components can follow.

**Database:** `db/schema.sql` (accounts, posts, reposts, follows, clusters, cluster_members,
cluster_posts, evidence, eval_runs) plus `db/migrations/` (applied by `python -m common.migrate`).
Migration 002 adds `likes`, `account_events`, `ingest_state`, `campaigns`,
`cluster_posts_snapshot`, enrichment columns on `accounts` and detector columns on `clusters`
(`campaign_id, p_value, families_fired, detector_version, window_kind, size, synthetic`), and makes
the raw tables hour-partitioned by `seen_at` (Jetstream server time). Details: `ingest/README.md`. Cluster `status` is `coordinated | likely_organic | insufficient`;
evidence `kind` is `signal | media | source | analyst`.

**API** (JSON):
- `GET /api/health` → `{ok, ingest_lag_s, last_event_at}` *(implemented)*
- `GET /api/clusters?status=&min_score=&limit=&include_synthetic=` → cluster summaries, newest first, each with `top_signals` and the latest analyst `verdict` *(implemented)*
- `GET /api/clusters/{id}` → summary + `signals` + `members` + `posts` (25, with bsky.app links) + `evidence` (signal/media/source) + `analyst` (full latest verdict) *(implemented)*
- `GET /api/metrics` → latest `eval_runs` per (dataset, split) *(implemented)*
- `GET /api/feed.atom?status=&min_score=&limit=&include_synthetic=` → Atom 1.0 feed of the newest clusters with one status (default `coordinated`, real traffic only), each entry linking to its dashboard page; follow new calls in any feed reader *(implemented)*
- `POST /api/lookup` `{url}` or `{text}` (an `@handle` as text is looked up as that Bluesky profile) → 202 + `LookupResult` (`state: running`); `GET /api/lookup/{id}` polls it until `done`. Live, receipted trace of one link; schema in `lookup/README.md` *(implemented)*
- `POST /api/analyze` `{text?, url?, handle?}` → `{status: matched|insufficient, clusters, author_clusters, web_findings, notes}`. `matched` = the input is in a monitored cluster (its verdicts apply); otherwise live Oxylabs findings only, never a verdict. `url` may be a web page, a Bluesky post or profile link (`bsky.app/profile/<handle|did>[/post/<rkey>]`) or an `at://` post URI; a post is matched against cluster posts, and when only its author is in a cluster that cluster is listed in `author_clusters` (context about the account, never `matched`). Control characters are removed from the input *(implemented)*
- `GET /api/swarms?limit=24&verdict=all|swarm|organic|unclear` → `SwarmWall` `{items: [SwarmCard], as_of, window_h}`: pictures that >= 5 Bluesky accounts posted in the last 24 h (`swarm/live.py`, worker `python -m swarm run`, table `meme_swarms` from migration 005). Only groups safe for a public page and updated by a pass in the last 20 min (`SWARM_FRESH_MIN`): no adult tags/words, no AppView porn/sexual/nudity/graphic-media labels on representative posts that the AppView really returned, a vision read of the picture shown that found nothing adult (no vision key: nothing new is shown unless `SWARM_WALL_WITHOUT_VISION=1`). Unsafe is sticky per file. Order: memes first, then `swarm` verdicts, by score; a tile that hashes like one already listed is skipped. Empty wall (not an error) before the migration has run; 503 when the DB is down *(implemented)*
- `GET /api/swarms/{id}` → `SwarmDetail` (card + `picture` = `/api/swarms/{id}/picture`, the `report` without examples on account/boost signals, <= 30 `posts` as links, `caption`); 404 when missing, stale or not safe to show. Shapes in `swarm/model.py`; a verdict describes the group of posts, never an account *(implemented)*
- `GET /api/swarms/{id}/picture` → the group's full-size picture (jpeg/png/webp only, `Cache-Control: private, max-age=300`, `nosniff`), fetched from the Bluesky CDN URL built from the stored row, cached in memory by blob CID; 404 like the detail, 502 when the CDN fails, 503 when busy *(implemented)*

**Analyst verdict** (stored in `evidence.detail` with `kind='analyst'`):
```json
{"status": "coordinated|likely_organic|insufficient", "confidence": 0.0,
 "evidence": [{"evidence_id": 1, "why": "..."}],
 "counter_evidence": ["..."], "would_change_verdict": ["..."], "model": "gemini-..."}
```
Also written: `summary` (1-2 sentences), `detector_status` (clusters.status at the time),
`adjustments` (what `analyst/guard.py` changed: invented citation ids are dropped, fewer than two
valid citations make the model's assessment `insufficient`), `cluster_updated_at`.
Since 2026-11-13 (late) `status` is always the detector's status; the model's own call is
`llm_assessment`, with `agrees_with_detector` and `disagreement_reason`. `tool_calls` and
`model_calls` trace Gemini's function calls (tool, endpoint, URL, evidence id, latency) and model
calls (model id, latency, tokens, cost). Details in `analyst/README.md`.
Each run appends a new row; readers take the latest.

**Media evidence** (`evidence.kind=media`, written by `media/`): one row per `(cluster_id, media_hash)`;
`media_hash` is the Bluesky blob CID, while the OCR-template row uses the reserved key
`ocr-template` and lists its input CIDs in `detail.media_hashes`. Successful rows carry
`detail.status=complete`, the Nemotron description/OCR fields, `model`, `created_at`, the source post
URL, and a safe-to-link `detail.media_url` when available. Failed work is stored as
`detail.status=failed` with a safe `error_code` and `stage`, never the raw provider error. The API
normalizes old successful rows and malformed detail objects explicitly. The dashboard labels
observed CIDs without a result as pending; that label means no stored result exists yet, not that a
worker is currently active. Media evidence explains the detector's status and never changes it.

**Source evidence** (`evidence.kind=source`, written by `sources/`, upserted by `detail.key`):
`detail.type` is `linked_page` (Oxylabs `/scrape` AI extraction: `publisher, author, published_at,
headline, claims, shared_by_accounts, http_status`) or `web_search` (Oxylabs `/search` for the
headline or the cluster text: `query, hits[{url, title, snippet, published_hint}], earliest_dated`).
`source_url` is always the page the fact came from. `published_hint` is the date the search engine
shows, a hint and not proof of origin.

**Workers:** `python -m sources worker` and `python -m analyst worker` poll for clusters with
`score >= 0.5`. One-off runs: `python -m sources cluster <id>`, `python -m analyst prompt <id>`.

**Env vars:** see `.env.example`. Oxylabs uses `OXYLABS_WEB_API_KEY` (Bearer key for the Web API; it replaced `OXYLABS_USERNAME`/`OXYLABS_PASSWORD`). `GEMINI_MODEL` picks the analyst model.
