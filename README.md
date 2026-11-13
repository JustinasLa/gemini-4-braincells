# Noise Radar

Detects coordinated amplification (bot or campaign behaviour) on Bluesky from behaviour signals,
explains each flagged cluster with evidence, and reports its own measured error rates.

## Layout

| Dir | What it does |
|---|---|
| `ingest/` | Bluesky Jetstream consumer → Postgres (`posts`, `reposts`, `likes`, `follows`, `account_events`); record/replay |
| `signals/` | Coordination detector: windows, signals, clusters, score + confidence, abstention |
| `media/` | Nemotron Super VL: reads images/video frames, near-duplicate media |
| `sources/` | Oxylabs `/scrape` + `/search` on cluster links (source URLs kept with every fact) |
| `analyst/` | Gemini: per-cluster verdict with evidence and counter-evidence, or abstains |
| `eval/` | Precision / recall / F1 / FPR on held-out labelled data → `eval_runs` |
| `api/` | FastAPI backend |
| `web/` | Dashboard (`web/public/index.html` is a placeholder until the real app lands) |
| `common/` | Shared settings (`common.config`) and DB connections (`common.db`) |
| `db/schema.sql` | Database schema, applied by the Postgres container on first start |
| `deploy/` | Docker Compose + Caddy for the production VPS |

## Dev setup

```bash
# uv: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                      # creates .venv with all deps (Python 3.12)
cp .env.example .env         # fill in your own keys; never commit .env
uv run pytest
uv run ruff check . && uv run ruff format .
uv run uvicorn api.main:app --port <your port>   # e.g. GET /api/health
```

On the shared team server, dev servers must use ports 3000-3099 or 8000-8099 (`je ports`).
Add dependencies with `uv add <pkg>` and commit `uv.lock`.

## Deploy

Live at **https://noise-radar.srv2159172.hstgr.cloud** (Hostinger VPS, Ubuntu 24.04, Docker).
The VPS's Docker Manager already runs Traefik on :80/:443 with Let's Encrypt, so our Caddy sits
behind it (`deploy/docker-compose.hostinger.yml`). From any checkout with the deploy SSH key:

```bash
git fetch && HOSTINGER_HOST=srv2159172.hstgr.cloud HOSTINGER_SSH_KEY=~/.ssh/hostinger_ed25519 \
  deploy/deploy.sh                      # default ref: origin/main; pass a sha to roll back
curl https://noise-radar.srv2159172.hstgr.cloud/api/health
curl https://noise-radar.srv2159172.hstgr.cloud/version.txt   # deployed commit
```

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
- `GET /api/clusters?status=&min_score=&limit=` → cluster summaries
- `GET /api/clusters/{id}` → cluster + members + post sample + evidence + analyst verdict
- `GET /api/metrics` → latest `eval_runs` per dataset
- `POST /api/analyze` `{text?, url?, handle?}` → ad-hoc check

**Analyst verdict** (stored in `evidence.detail` with `kind='analyst'`):
```json
{"status": "coordinated|likely_organic|insufficient", "confidence": 0.0,
 "evidence": [{"evidence_id": 1, "why": "..."}],
 "counter_evidence": ["..."], "would_change_verdict": ["..."], "model": "gemini-..."}
```

**Env vars:** see `.env.example`.

## Rules

- Every number shown in the UI comes from `eval/` or `signals/` code, never hard-coded.
- Log every model call (model id, latency, tokens if returned).
- Read-only on Bluesky: never post or create accounts.
