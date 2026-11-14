# Shill or Chill

**Shill or chill? We check the receipts.**

Shill or Chill watches Bluesky live and finds groups of accounts that amplify the same thing
together, such as rings of fresh accounts pushing one link within seconds, reply floods or
like-farms. It explains every flagged group with evidence you can click through, says when
something is most likely just people reacting to the news, refuses to guess when the evidence is
thin, and publishes its own measured error rates.

Live: **https://noise-radar.srv2159172.hstgr.cloud** (Hostinger VPS). API health:
[`/api/health`](https://noise-radar.srv2159172.hstgr.cloud/api/health), measured accuracy:
[`/api/metrics`](https://noise-radar.srv2159172.hstgr.cloud/api/metrics).

Full method, metrics, limits and ethics: **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

Status: ingest, the detector, Oxylabs source checks, Nemotron media reading, Gemini verdicts and the
API are on `main` and live. The eval harness (open PRs #9 and #10) and the dashboard are being
integrated; until they land, `main` has only their stubs and `/api/metrics` may be empty.

## The thesis

The common way to fight "noise" is to paste a post into a language model and ask whether it is
propaganda. That judges opinions, can't be checked, and fails on exactly the cases that matter:
a true message pushed by a bot ring, or a false one shared by real, angry people.

We don't look at what a post says. We look at **how accounts act together**: the same text or
file from many accounts, bursts tighter than chance allows, the same accounts liking the same posts
again and again, accounts created last week with template handles. Coordination is a property of
a group, so every verdict is about a group, and every piece of evidence is a number with links to
the posts behind it.

- **Statistics decide, models explain.** The status of a cluster comes from fixed rules over four
  independent signal families and a permutation test against a null model. Gemini investigates
  (live Oxylabs searches and page fetches through function calling) and writes the explanation;
  code makes sure it cites only real evidence. If Gemini disagrees with the detector, that is
  shown and counted, never applied.
- **False positives are the main risk.** Breaking news and viral posts look like coordination.
  Popular items are dropped before clustering, organic guards produce counter-evidence, and
  conflicting evidence leads to "can't tell" instead of a flag.
- **Honest numbers.** Every accuracy figure in the app is produced by `eval/` on held-out data
  and served from the database. Nothing is typed in by hand.

## Verdicts

| Label | API value (`clusters.status`) | Meaning |
|---|---|---|
| 🚩 Shill | `coordinated` | At least two independent signal families fired, timing beats the null model, no strong organic explanation. |
| 😎 Chill | `likely_organic` | A strong organic explanation (established accounts, a wider crowd doing the same) and at most one suspicious family. |
| 🤷 Can't tell (yet) | `insufficient` | Everything else. We abstain. |

"Shill" labels a cluster's verdict, never a person: we can show that accounts acted together, not
that anyone was paid or why.

## Architecture

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
   or headline appears          (being integrated)          code-checked (analyst/guard.py)
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
| `lookup/` | Trace any link (X post, Bluesky post, profile, web page) with live data: Oxylabs `/scrape` reads the post, its counts and the author's profile; `/search` finds the same text on the web, on X and Bluesky and the coverage of the story; candidate copies are verified on their own pages (copy, quote or reply); exact X post times from the post id; Nemotron reads the images; Gemini writes a cited reading. Coordination verdict only when the link is in a monitored cluster. `lookup/README.md`. |
| `analyst/` | Gemini: per-cluster verdict JSON with cited evidence, counter-evidence and what would change it; guarded by code. |
| `eval/` | Recall on injected synthetic campaigns, false-positive rate on organic bursts, on held-out hours → `eval_runs`. In review (PRs #9, #10). |
| `api/` | FastAPI: clusters, cluster detail, metrics, health, ad-hoc `POST /api/analyze`. |
| `web/` | Dashboard served by Caddy (`web/public/index.html` is a placeholder until the dashboard lands). |
| `common/` | Shared settings (`common.config`) and DB connections (`common.db`), migrations runner. |
| `db/` | `schema.sql` plus `migrations/`. |
| `deploy/` | Docker Compose, Caddy, deploy and outside-check scripts for the Hostinger VPS. |

## How the metrics are made

The eval code is in review (PRs #9 and #10); this describes what it does. `eval/` replays recorded Jetstream hours offline, through the same normaliser and the same
detector entry point as the live worker, and writes one `eval_runs` row per dataset and split.
`GET /api/metrics` returns the latest row per dataset and split, unchanged.

- **Recall**: synthetic campaigns (three difficulty levels) are injected one at a time into real
  recorded windows; a campaign counts as caught when most of its accounts land in `coordinated`
  clusters. Reported with a 95% Wilson interval.
- **False-positive rate**: synthetic hard negatives (viral post, news link, live event, fandom)
  and real organic bursts picked by a fixed rule that never looks at detector output.
- **Split**: odd UTC hours are held out, even hours are for development. We tune only on dev.
- Each row's `details` records the detector version and the eval code commit.
- An empty `/api/metrics` means no run has been published to that deployment yet, so there are
  no accuracy numbers to show yet.

Live precision and confidence calibration need hand-labelled live clusters; they aren't reported
until those labels exist. Details: [docs/METHODOLOGY.md](docs/METHODOLOGY.md#how-the-metrics-are-made).

## Limits

Short version (the full list is in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md#known-failure-modes-and-limits)):

- Slow-drip campaigns with aged accounts and paraphrased text mostly come out "can't tell".
- Only timing has a null model; content and identity base rates aren't modelled yet.
- Declared automation (news feeds, schedulers, one publisher's several feed accounts) can look
  coordinated; guards catch some of it, not all.
- Rings that only push items that are already viral are invisible, because popular items are
  dropped to protect breaking news.
- Very short texts and unspaced scripts (Chinese, Japanese, Thai) don't produce text keys.
- `confidence` is a heuristic until it's calibrated on labelled data.

## Ethics

We flag **behaviour, not opinions**: what a message says is never a signal. We show coordination,
never payment or intent, and no account is ever called "a shill". Every verdict shows its
counter-evidence and the rule behind it. Analysis is **read-only**: we never post, like, follow or
create accounts on Bluesky, and test campaigns exist only in offline replays labelled synthetic.
We use public data only and don't try to identify the people behind accounts.

## Run locally

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and a Postgres 16 with pgvector (or Docker).

```bash
# uv: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                      # creates .venv with all deps (Python 3.12)
cp .env.example .env         # fill in your own keys; never commit .env
uv run pytest
uv run ruff check . && uv run ruff format .
uv run uvicorn api.main:app --port <your port>   # e.g. GET /api/health
```

The whole pipeline, against the database in `DATABASE_URL`:

```bash
uv run python -m common.migrate            # schema.sql + db/migrations/*.sql (idempotent)
uv run python -m ingest run                # Jetstream → Postgres (add --record DIR to keep raw files)
uv run python -m signals run               # detector worker (10m window every minute, 1h every 5 min)
uv run python -m sources worker            # Oxylabs checks for clusters with score >= 0.5
uv run python -m analyst worker            # Gemini verdicts for clusters with score >= 0.5
uv run uvicorn api.main:app --port <your port>
```

One-off and offline tools: `python -m signals once --window 10m --dry-run` (one detector pass,
writes nothing), `python -m sources cluster <id> --dry-run`, `python -m analyst prompt <id>` (shows
the prompt), `python -m ingest replay DIR --database-url URL` (recorded files into another DB).
Keys you need: `GEMINI_API_KEY` + `GEMINI_MODEL` (analyst), `OXYLABS_WEB_API_KEY` (sources and the
web part of `/api/analyze`), `VERDA_INFERENCE_KEY` or `NVIDIA_API_KEY` (media; `python -m media cluster <id> --dry-run`). Without them the detector and API still run.

Or run the whole stack with Docker on a machine without another web server on :80/:443 (see
Deploy for the Hostinger override): `docker compose -f deploy/docker-compose.yml --env-file .env up -d --build`.

On the shared team server, dev servers must use ports 3000-3099 or 8000-8099 (`je ports`).
Add dependencies with `uv add <pkg>` and commit `uv.lock`.

## Deploy

Live at **https://noise-radar.srv2159172.hstgr.cloud** (Hostinger VPS, Ubuntu 24.04, Docker).
The VPS's Docker Manager already runs Traefik on :80/:443 with Let's Encrypt, so our Caddy sits
behind it (`deploy/docker-compose.hostinger.yml`). From any checkout with the deploy SSH key:

```bash
git fetch && HOSTINGER_HOST=srv2159172.hstgr.cloud HOSTINGER_SSH_KEY=~/.ssh/hostinger_ed25519 \
  CHECK_URL=https://noise-radar.srv2159172.hstgr.cloud \
  deploy/deploy.sh                      # default ref: origin/main; pass a sha to roll back
deploy/check.sh                         # outside check: health + ingest lag, clusters, metrics, web
curl https://noise-radar.srv2159172.hstgr.cloud/version.txt   # deployed commit
```

Redeploy after every merge to `main`. `deploy/check.sh` also runs every 5 minutes from the team
server (cron of the deploying user) and logs to `~/noise-radar-health.log`.

Running it on the VPS: every long-running service has `restart: unless-stopped` (`migrate` is a
one-shot and exits). Raw Jetstream tables keep `INGEST_RETENTION_HOURS` of data (likes:
`INGEST_LIKES_RETENTION_HOURS`), set in the VPS `.env`; at ~0.7 GB/hour of raw rows that bounds the
database, and anything a cluster references is copied to `cluster_posts_snapshot` first. The signals
worker gets 4G on the VPS (`docker-compose.hostinger.yml`) because the 1h window needs more than 2G.

`deploy.sh` ships the committed tree with `git archive` (the VPS has no GitHub access) into
`/opt/noise-radar/releases/<sha>`, points `current` at it and runs compose. Secrets live only in
`/opt/noise-radar/.env` on the VPS (same names as `.env.example`); edit it there, then redeploy.

On a plain VPS without Traefik, drop the override and set `PUBLIC_DOMAIN` so Caddy gets HTTPS itself:
`docker compose -f deploy/docker-compose.yml --env-file .env up -d --build`.

All backend services share one image (`Dockerfile`); add a service in compose with its own
`command` (e.g. `python -m ingest`).

## Entire (agent sessions, Graph, Brain)

The repo is set up for [Entire](https://docs.entire.com): Claude Code and Codex sessions are linked to
commits as checkpoints (stored under `refs/entire/checkpoints/` and pushed with your normal `git push`),
and `.entire/agent-guide.md` tells agents to use `entire graph` for code lookups.
The hooks do nothing until `entire` is on your PATH, so nothing breaks without it. To take part:

```bash
curl -fsSL https://entire.com/install.sh | bash   # installs entire into ~/.local/bin
entire login                                      # GitHub sign-in (use --device on a server)
entire plugin install graph && entire plugin install brain --yes
```

Then start a new agent session in your worktree. Codex users: approve the Entire hooks once in an
interactive Codex session (`/hooks`), then check with `entire doctor`. Checkpoints contain agent transcripts: never paste
keys or `.env` contents into a prompt. Committed Entire files: `.entire/settings.json`,
`.entire/.gitignore`, `.entire/agent-guide.md`, `.claude/settings.json`, `.codex/hooks.json`,
`AGENTS.md`, `CLAUDE.md` (Entire manages the marked blocks; add your own text outside them).

## Shared interfaces

Change these only with a note here, so the other components can follow.

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
- `POST /api/lookup` `{url}` or `{text}` → 202 + `LookupResult` (`state: running`); `GET /api/lookup/{id}` polls it until `done`. Live, receipted trace of one link; schema in `lookup/README.md` *(implemented)*
- `POST /api/analyze` `{text?, url?, handle?}` → `{status: matched|insufficient, clusters, web_findings, notes}`. `matched` = the input is in a monitored cluster (its verdicts apply); otherwise live Oxylabs findings only, never a verdict *(implemented)*

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

**Source evidence** (`evidence.kind=source`, written by `sources/`, upserted by `detail.key`):
`detail.type` is `linked_page` (Oxylabs `/scrape` AI extraction: `publisher, author, published_at,
headline, claims, shared_by_accounts, http_status`) or `web_search` (Oxylabs `/search` for the
headline or the cluster text: `query, hits[{url, title, snippet, published_hint}], earliest_dated`).
`source_url` is always the page the fact came from. `published_hint` is the date the search engine
shows, a hint and not proof of origin.

**Workers:** `python -m sources worker` and `python -m analyst worker` poll for clusters with
`score >= 0.5`. One-off runs: `python -m sources cluster <id>`, `python -m analyst prompt <id>`.

**Env vars:** see `.env.example`. Oxylabs uses `OXYLABS_WEB_API_KEY` (Bearer key for the Web API; it replaced `OXYLABS_USERNAME`/`OXYLABS_PASSWORD`). `GEMINI_MODEL` picks the analyst model.

## Rules

- Every number shown in the UI comes from `eval/` or `signals/` code, never hard-coded.
- Log every model call (model id, latency, tokens if returned).
- Read-only on Bluesky: never post or create accounts.
