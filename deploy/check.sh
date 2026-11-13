#!/usr/bin/env bash
# Outside health check for the public site. Exit 0 = healthy; prints one line either way.
#   deploy/check.sh [base-url]      # default https://noise-radar.srv2159172.hstgr.cloud
# Env: MAX_LAG_S (default 300): /api/health fails when the newest ingested post is older than this.
set -uo pipefail

url=${1:-https://noise-radar.srv2159172.hstgr.cloud}
max_lag=${MAX_LAG_S:-300}
fail=()
get() { curl -fsS --max-time 20 "$url$1"; }

health=$(get /api/health) || fail+=("/api/health unreachable")
if [[ -n ${health:-} ]]; then
  python3 -c 'import json,sys; h=json.loads(sys.argv[1]); lag=h.get("ingest_lag_s"); sys.exit(0 if h.get("ok") and lag is not None and lag < float(sys.argv[2]) else 1)' \
    "$health" "$max_lag" || fail+=("health not ok: $health")
fi
for path in /api/clusters?limit=1 /api/metrics /; do
  get "$path" >/dev/null || fail+=("$path failed")
done
version=$(get /version.txt 2>/dev/null | tr -d '\n') || version=unknown

ts=$(date -u +%FT%TZ)
if ((${#fail[@]})); then
  echo "$ts FAIL $url version=$version: ${fail[*]}"
  exit 1
fi
echo "$ts OK $url version=$version $health"
