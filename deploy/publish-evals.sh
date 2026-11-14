#!/usr/bin/env bash
# Rerun the published evals on the detector the public site runs and write their eval_runs rows to
# its database, so /api/metrics (the dashboard's Accuracy page) shows numbers for that detector.
#   deploy/publish-evals.sh         # from a clean checkout of the commit the site runs (/version.txt)
# Run it after every deploy that changes signals VERSION; deploy/check.sh warns when /api/metrics
# is empty or its rows name another detector version. Runs on the team server: it needs the
# recorded slices (EVAL_SLICES_DIR, default /srv/junction-espoo/data/eval-slices) and the prod DB
# tunnel (deploy/db-tunnel.sh: HOSTINGER_HOST, HOSTINGER_SSH_KEY). The four runs go in parallel
# (~5-10 min). Rows: synthetic_campaigns dev+heldout, organic_synth + organic_live heldout,
# calibration heldout. Live hand labels are scored separately (`eval labels score`) once labelled.
# Env: CHECK_URL (default the public site), BASE_RATES (measured base rates for the identity test,
#      as the live worker runs it), RESULTS_DIR (default $EVAL_SLICES_DIR/results), WORKERS (3),
#      DRY_RUN=1 (result JSON only, nothing written to any database).
set -euo pipefail
cd "$(dirname "$0")/.."

url=${CHECK_URL:-https://noise-radar.srv2159172.hstgr.cloud}
slices=${EVAL_SLICES_DIR:-/srv/junction-espoo/data/eval-slices}
rates=${BASE_RATES:-signals/fixtures/attack_results_live_v15_20261113T2000Z.json}
out=${RESULTS_DIR:-$slices/results}
workers=${WORKERS:-3}

if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo "uncommitted changes: run from a clean checkout of the deployed commit" >&2
  exit 1
fi
live=$(curl -fsS --max-time 20 "$url/version.txt" | tr -d '[:space:]')
head=$(git rev-parse --short=12 HEAD)
if [[ $head != "$live" ]]; then
  echo "this checkout is $head but $url runs '$live': check out that commit first" >&2
  exit 1
fi
version=$(uv run --frozen python -c 'from signals.config import DEFAULT; print(DEFAULT.version)')

db=()
if [[ -z ${DRY_RUN:-} ]]; then
  deploy/db-tunnel.sh start
  set -a
  # shellcheck disable=SC1090
  . "${PROD_DB_ENV:-$HOME/.config/noise-radar/prod-db.env}"
  set +a
  db=(--database-url "$PROD_DATABASE_URL")
fi
write=()
((${#db[@]})) && write=(--write)

mkdir -p "$out"
echo "detector $version at $head -> ${DRY_RUN:+DRY RUN, }results in $out"
declare -A pids
run() { # name, eval args...
  local name=$1
  shift
  EVAL_SLICES_DIR=$slices uv run --frozen python -m eval "$@" >"$out/$name-$version.log" 2>&1 &
  pids[$name]=$!
}
for split in heldout dev; do
  run "synthetic_campaigns-$split" recall --split "$split" --slice "$slices/jetstream" \
    --base-rates "$rates" --workers "$workers" --json-out "$out/synthetic_campaigns-$split-$version.json" "${db[@]}"
done
run organic-heldout organic run --split heldout --base-rates "$rates" --workers "$workers" \
  --json "$out/organic-heldout-$version.json" "${write[@]}" "${db[@]}"
run calibration-heldout calibration --split heldout --slice "$slices/jetstream" --base-rates "$rates" \
  --json-out "$out/calibration-heldout-$version.json" "${db[@]}"

failed=()
for name in "${!pids[@]}"; do
  if wait "${pids[$name]}"; then echo "ok   $name"; else failed+=("$name"); echo "FAIL $name (see $out/$name-$version.log)"; fi
done

curl -fsS --max-time 20 "$url/api/metrics" | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    d = r.get("details") or {}
    rate = next((f"{k}={r[k]}" for k in ("recall", "fpr", "precision") if r.get(k) is not None), "")
    print(f"live {r[\"dataset\"]:20} {r[\"split\"]:8} {d.get(\"detector_version\")} n={r[\"n\"]} {rate}")'
((${#failed[@]} == 0))
