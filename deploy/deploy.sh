#!/usr/bin/env bash
# Ship a committed git ref to the Hostinger VPS and (re)start the stack there.
#   deploy/deploy.sh [ref]          # default: origin/main (run `git fetch` first)
# Env: HOSTINGER_HOST (required), HOSTINGER_USER (default root), HOSTINGER_SSH_KEY (optional key file),
#      HOSTINGER_DIR (default /opt/noise-radar).
# The VPS needs no GitHub access: the tree goes over SSH with `git archive`, so only committed files
# ship. Each deploy lands in $DIR/releases/<sha>, `current` points at it, and $DIR/.env (created once
# by hand on the VPS, never committed) is linked into every release. Roll back: deploy an older sha.
set -euo pipefail

ref=${1:-origin/main}
host=${HOSTINGER_HOST:?set HOSTINGER_HOST}
user=${HOSTINGER_USER:-root}
dir=${HOSTINGER_DIR:-/opt/noise-radar}
ssh_opts=(-o BatchMode=yes)
if [[ -n ${HOSTINGER_SSH_KEY:-} ]]; then ssh_opts+=(-i "$HOSTINGER_SSH_KEY" -o IdentitiesOnly=yes); fi

sha=$(git rev-parse --short=12 "$ref^{commit}")
echo "deploying $ref ($sha) to $user@$host:$dir"

# 1) upload the tree into releases/<sha>.tmp, unless that release is already on the VPS
if ssh "${ssh_opts[@]}" "$user@$host" "test -d '$dir/releases/$sha'"; then
  echo "release $sha already on the VPS, skipping upload"
else
  git archive --format=tar "$sha" | ssh "${ssh_opts[@]}" "$user@$host" \
    "set -e; rm -rf '$dir/releases/$sha.tmp'; mkdir -p '$dir/releases/$sha.tmp'; tar -x -C '$dir/releases/$sha.tmp'"
fi

# 2) build, start, and only then point `current` at it
ssh "${ssh_opts[@]}" "$user@$host" bash -s -- "$dir" "$sha" <<'REMOTE'
set -euo pipefail
dir=$1 sha=$2
rel=$dir/releases/$sha
test -f "$dir/.env" || { echo "missing $dir/.env on the VPS (copy .env.example and fill it in)" >&2; exit 1; }
exec 9>"$dir/.deploy.lock"
flock -w 600 9 || { echo "another deploy is running" >&2; exit 1; }
if [[ -d $rel.tmp ]]; then
  ln -sfn ../../.env "$rel.tmp/.env"
  echo "$sha" > "$rel.tmp/web/public/version.txt"
  rm -rf "$rel" && mv "$rel.tmp" "$rel"
fi
touch "$rel"   # newest mtime, so pruning below never removes the live release
# Run compose from the real release path (not the `current` symlink): bind-mount paths then change
# per release, so compose recreates the containers that mount repo files (Caddy, db init script).
cd "$rel"
dc=(docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostinger.yml --env-file .env)
"${dc[@]}" build
"${dc[@]}" up -d --remove-orphans
ln -sfn "releases/$sha" "$dir/current"
docker image prune -f --filter label=com.docker.compose.project=noise-radar >/dev/null
# keep the 5 newest finished releases
ls -1dt "$dir"/releases/*/ | { grep -v '\.tmp/$' || true; } | tail -n +6 | xargs -r rm -rf
"${dc[@]}" ps
REMOTE
