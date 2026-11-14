#!/usr/bin/env bash
# Outside health check for the public site. Exit 0 = healthy; media coverage is advisory.
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
trackdown=$(curl -sS --max-time 20 -w $'\n%{http_code}' "$url/trackdown/")
if [[ $? != 0 || ${trackdown##*$'\n'} != 200 ]] ||
  ! grep -Eiq '<(div|script)([[:space:]>])' <<< "${trackdown%$'\n'*}"; then
  fail+=("/trackdown/ must return HTTP 200 and HTML containing div or script")
fi

media_selection() {
  if command -v jq >/dev/null 2>&1; then
    jq -er '.[0:50] | length, (.[0:5][] | .id | numbers)'
  else
    python3 -c 'import json,sys; rows=json.load(sys.stdin)[:50]; print(len(rows));
for row in rows[:5]:
    ident=row["id"]
    if type(ident) is not int or ident < 0: raise ValueError("invalid cluster id")
    print(ident)'
  fi
}
media_count() {
  if command -v jq >/dev/null 2>&1; then
    jq -er '[.evidence[] | select(.kind == "media")] | length'
  else
    python3 -c 'import json,sys; print(sum(e["kind"] == "media" for e in json.load(sys.stdin)["evidence"]))'
  fi
}
check_media() {
  local listing selection latest ident detail count
  local checked=0 found=0 unavailable=0
  listing=$(get '/api/clusters?limit=50') &&
    selection=$(media_selection <<< "$listing") || {
      echo "WARN media evidence coverage unavailable: cluster list could not be read"
      return
    }
  latest=${selection%%$'\n'*}
  if [[ $selection == *$'\n'* ]]; then
    while read -r ident; do
      [[ $ident =~ ^[0-9]+$ ]] || continue
      if detail=$(get "/api/clusters/$ident") && count=$(media_count <<< "$detail"); then
        checked=$((checked + 1))
        ((count > 0)) && found=$((found + 1))
      else
        unavailable=$((unavailable + 1))
      fi
    done <<< "${selection#*$'\n'}"
  fi
  echo "INFO media evidence clusters=$found/$checked checked (latest=$latest; unavailable=$unavailable; at most 5 details sampled)"
  if ((found == 0 || unavailable > 0)); then
    echo "WARN media evidence absent or incomplete in sample; quiet hours may have no candidate media"
  fi
}
check_media
version=$(get /version.txt 2>/dev/null | tr -d '\n') || version=unknown

ts=$(date -u +%FT%TZ)
if ((${#fail[@]})); then
  echo "$ts FAIL $url version=$version: ${fail[*]}"
  exit 1
fi
echo "$ts OK $url version=$version $health"
