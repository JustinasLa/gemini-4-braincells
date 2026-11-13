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

# 1) upload the tree into releases/<sha>.tmp
git archive --format=tar "$sha" | ssh "${ssh_opts[@]}" "$user@$host" \
  "set -e; rm -rf '$dir/releases/$sha.tmp'; mkdir -p '$dir/releases/$sha.tmp'; tar -x -C '$dir/releases/$sha.tmp'"

# 2) activate it and (re)start the stack
ssh "${ssh_opts[@]}" "$user@$host" bash -s -- "$dir" "$sha" <<'REMOTE'
set -euo pipefail
dir=$1 sha=$2
rel=$dir/releases/$sha
test -f "$dir/.env" || { echo "missing $dir/.env on the VPS (copy .env.example and fill it in)" >&2; exit 1; }
if [[ -d $rel ]]; then
  rm -rf "$rel.tmp"   # same sha again: keep the dir running containers already mount
else
  ln -sfn ../../.env "$rel.tmp/.env"
  echo "$sha" > "$rel.tmp/web/public/version.txt"
  mv "$rel.tmp" "$rel"
fi
touch "$rel"          # newest mtime, so pruning below never removes the live release
ln -sfn "releases/$sha" "$dir/current"
cd "$dir/current"
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostinger.yml --env-file .env up -d --build --remove-orphans
docker image prune -f >/dev/null
# keep the 5 newest releases
ls -1dt "$dir"/releases/*/ | tail -n +6 | xargs -r rm -rf
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostinger.yml --env-file .env ps
REMOTE
