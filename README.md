# Noise Radar

Detects coordinated amplification (bot or campaign behaviour) on Bluesky from behaviour signals,
explains each flagged cluster with evidence, and reports its own measured error rates.

## Layout

| Dir | What it does |
|---|---|
| `ingest/` | Bluesky Jetstream consumer → Postgres (`accounts`, `posts`, `reposts`, `follows`) |
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

On the VPS, with `.env` at the repo root (set `PUBLIC_DOMAIN` for automatic HTTPS):

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
curl https://$PUBLIC_DOMAIN/api/health
```

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

Then start a new agent session in your worktree. Checkpoints contain agent transcripts: never paste
keys or `.env` contents into a prompt. Committed Entire files: `.entire/settings.json`,
`.entire/.gitignore`, `.entire/agent-guide.md`, `.claude/settings.json`, `.codex/hooks.json`,
`AGENTS.md`, `CLAUDE.md` (Entire manages the marked blocks; add your own text outside them).

## Shared interfaces

Change these only with a note here, so the other components can follow.

**Database:** `db/schema.sql` (accounts, posts, reposts, follows, clusters, cluster_members,
cluster_posts, evidence, eval_runs). Cluster `status` is `coordinated | likely_organic | insufficient`;
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
