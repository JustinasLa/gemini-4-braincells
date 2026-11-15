# Development and deployment

Running the stack locally, deploying it, and the team's agent tooling.

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
deploy/smoke.sh [base-url]                # final API checklist; defaults to production
curl https://noise-radar.srv2159172.hstgr.cloud/version.txt   # deployed commit
```

Redeploy after every merge to `main`. When the deploy changes `signals/config.py` `VERSION` (or
`/api/metrics` is empty: `check.sh` warns about both), republish the accuracy numbers from the
team server, from a clean checkout of the deployed commit (~5-10 min):

```bash
git checkout --detach "$(curl -s https://noise-radar.srv2159172.hstgr.cloud/version.txt)"
HOSTINGER_HOST=srv2159172.hstgr.cloud HOSTINGER_SSH_KEY=~/.ssh/hostinger_ed25519 deploy/publish-evals.sh
```

It reruns recall (dev + heldout), organic FPR and calibration (heldout) on the recorded slices,
writes their `eval_runs` rows to the prod DB and keeps the result JSON in
`/srv/junction-espoo/data/eval-slices/results/`. `deploy/check.sh` also runs every 5 minutes from the team
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

## Rules

- Every number shown in the UI comes from `eval/` or `signals/` code, never hard-coded.
- Log every model call (model id, latency, tokens if returned).
- Read-only on Bluesky: never post or create accounts.
