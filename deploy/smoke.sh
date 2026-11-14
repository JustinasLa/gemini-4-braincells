#!/bin/sh
# Final pre-demo API smoke test; POSIX sh plus curl and Python 3 for JSON validation.
# Usage: deploy/smoke.sh [base-url]
# The lookup check submits one safe https://example.com/ URL and polls its in-memory job.
set -u

if [ "$#" -gt 1 ]; then
  printf '%s\n' "Usage: $0 [base-url]" >&2
  exit 2
fi
if [ "$#" -eq 0 ]; then
  BASE_URL=https://noise-radar.srv2159172.hstgr.cloud
else
  BASE_URL=$1
fi
BASE_URL=$(printf '%s' "$BASE_URL" | sed 's:/*$::')

for tool in curl python3 tr sed cut wc date sleep; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'FAIL required tool is unavailable: %s\n' "$tool" >&2
    exit 2
  fi
done

if ! printf '%s' "$BASE_URL" | python3 -c '
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.stdin.read())
sys.exit(0 if url.scheme in ("http", "https") and url.hostname and
         not url.username and not url.password and not url.query and not url.fragment else 1)
' >/dev/null 2>&1; then
  printf '%s\n' "FAIL base URL must be http(s), without credentials, query, or fragment" >&2
  exit 2
fi

hard_failures=0
warnings=0
pass() { printf 'PASS %s\n' "$1"; }
warn() { warnings=$((warnings + 1)); printf 'WARN %s\n' "$1"; }
fail() { hard_failures=$((hard_failures + 1)); printf 'FAIL %s\n' "$1"; }
get() { curl -fsS --connect-timeout 5 --max-time 20 "$BASE_URL$1"; }

printf '%s\n' "INFO Shill or Chill production smoke checklist"

if version_raw=$(get /version.txt 2>/dev/null); then
  version=$(printf '%s' "$version_raw" | tr -d '[:space:]')
  version_length=$(printf '%s' "$version" | wc -c | tr -d '[:space:]')
  case "$version" in
    ''|*[!0123456789abcdefABCDEF]*) fail "/version.txt is empty or not a hexadecimal commit SHA" ;;
    *)
      if [ "$version_length" -lt 7 ] || [ "$version_length" -gt 64 ]; then
        fail "/version.txt SHA length is invalid"
      else
        pass "/version.txt commit=$version"
      fi
      ;;
  esac
else
  fail "/version.txt did not respond successfully"
fi

if health=$(get /api/health 2>/dev/null) &&
   printf '%s' "$health" | python3 -c '
import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(value,dict) and value.get("ok") is True else 1)
' >/dev/null 2>&1; then
  pass "/api/health JSON ok=true"
else
  fail "/api/health did not return JSON with ok=true"
fi

if metrics=$(get /api/metrics 2>/dev/null); then
  metrics_count=$(printf '%s' "$metrics" | python3 -c '
import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(value,list):
    sys.exit(1)
print(len(value))
' 2>/dev/null) || metrics_count=
  case "$metrics_count" in
    ''|*[!0-9]*) fail "/api/metrics did not return a JSON list" ;;
    0) warn "/api/metrics is valid but empty; the Accuracy page has no evaluation rows" ;;
    *) pass "/api/metrics valid JSON list rows=$metrics_count" ;;
  esac
else
  fail "/api/metrics did not respond successfully"
fi

if clusters=$(get '/api/clusters?limit=1' 2>/dev/null); then
  cluster_meta=$(printf '%s' "$clusters" | python3 -c '
import json,sys
try:
    rows=json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(rows,list):
    sys.exit(1)
if not rows:
    print("0|")
else:
    row=rows[0]
    ident=row.get("id") if isinstance(row,dict) else None
    if isinstance(ident,bool) or not isinstance(ident,int) or ident < 0:
        sys.exit(1)
    print(str(len(rows))+"|"+str(ident))
' 2>/dev/null) || cluster_meta=
  case "$cluster_meta" in
    ''|*[!0-9\|]*) fail "/api/clusters?limit=1 did not return a JSON list with a valid first cluster id" ;;
    *)
      cluster_count=$(printf '%s' "$cluster_meta" | cut -d'|' -f1)
      cluster_id=$(printf '%s' "$cluster_meta" | cut -d'|' -f2)
      if [ "$cluster_count" -eq 0 ]; then
        warn "/api/clusters?limit=1 returned an empty list; detail check skipped"
      elif [ -z "$cluster_id" ]; then
        fail "/api/clusters?limit=1 returned no usable cluster id"
      else
        pass "/api/clusters?limit=1 valid JSON list rows=$cluster_count"
        if detail=$(get "/api/clusters/$cluster_id" 2>/dev/null); then
          evidence_count=$(printf '%s' "$detail" | python3 -c '
import json,sys
try:
    value=json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(value,dict) or value.get("id") != int(sys.argv[1]):
    sys.exit(1)
evidence=value.get("evidence")
if not isinstance(evidence,list):
    sys.exit(1)
print(len(evidence))
' "$cluster_id" 2>/dev/null) || evidence_count=
          case "$evidence_count" in
            ''|*[!0-9]*) fail "/api/clusters/$cluster_id detail JSON is invalid or lacks an evidence list" ;;
            0)
              pass "/api/clusters/$cluster_id detail JSON has an evidence list"
              warn "/api/clusters/$cluster_id currently has no evidence rows"
              ;;
            *) pass "/api/clusters/$cluster_id detail JSON evidence_rows=$evidence_count" ;;
          esac
        else
          fail "/api/clusters/$cluster_id did not respond successfully"
        fi
      fi
      ;;
  esac
else
  fail "/api/clusters?limit=1 did not respond successfully"
fi

lookup_metadata() {
  printf '%s' "$1" | python3 -c '
import json,re,sys
try:
    value=json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(value,dict):
    sys.exit(1)
ident=value.get("id")
state=value.get("state")
if not isinstance(ident,str) or not re.fullmatch(r"lk_[0-9a-f]{16}",ident):
    sys.exit(1)
if state not in ("running","done","failed"):
    sys.exit(1)
expected=sys.argv[1]
if expected != "-" and ident != expected:
    sys.exit(1)
print(ident+"|"+state)
' "$2" 2>/dev/null
}

printf '%s\n' "INFO submitting safe lookup URL https://example.com/"
if lookup_body=$(curl -fsS --connect-timeout 5 --max-time 20 \
  -H 'Content-Type: application/json' \
  --data-binary '{"url":"https://example.com/"}' \
  "$BASE_URL/api/lookup" 2>/dev/null); then
  lookup_meta=$(lookup_metadata "$lookup_body" -) || lookup_meta=
  if [ -z "$lookup_meta" ]; then
    fail "POST /api/lookup did not return valid JSON with a lookup id and state"
  else
    lookup_id=$(printf '%s' "$lookup_meta" | cut -d'|' -f1)
    lookup_state=$(printf '%s' "$lookup_meta" | cut -d'|' -f2)
    polls=0
    if [ "$lookup_state" = running ]; then
      start_time=$(date +%s 2>/dev/null)
      case "$start_time" in
        ''|*[!0-9]*)
          fail "system clock unavailable; lookup timeout cannot be enforced"
          lookup_state=error
          ;;
        *)
          deadline=$((start_time + 180))
          while [ "$lookup_state" = running ]; do
            now=$(date +%s 2>/dev/null)
            case "$now" in
              ''|*[!0-9]*)
                fail "system clock unavailable while polling lookup"
                lookup_state=error
                break
                ;;
            esac
            if [ "$now" -ge "$deadline" ]; then
              printf 'FAIL lookup job timed out after 180s (polls=%s)\n' "$polls"
              hard_failures=$((hard_failures + 1))
              lookup_state=error
              break
            fi
            sleep 2
            polls=$((polls + 1))
            if lookup_body=$(curl -fsS --connect-timeout 5 --max-time 10 \
              "$BASE_URL/api/lookup/$lookup_id" 2>/dev/null); then
              lookup_meta=$(lookup_metadata "$lookup_body" "$lookup_id") || lookup_meta=
              if [ -z "$lookup_meta" ]; then
                fail "GET /api/lookup/$lookup_id returned invalid JSON or a mismatched job id"
                lookup_state=error
              else
                now=$(date +%s 2>/dev/null)
                case "$now" in
                  ''|*[!0-9]*)
                    fail "system clock unavailable while polling lookup"
                    lookup_state=error
                    ;;
                  *)
                    if [ "$now" -ge "$deadline" ]; then
                      printf 'FAIL lookup job timed out after 180s (polls=%s)\n' "$polls"
                      hard_failures=$((hard_failures + 1))
                      lookup_state=error
                    else
                      lookup_state=$(printf '%s' "$lookup_meta" | cut -d'|' -f2)
                    fi
                    ;;
                esac
              fi
            else
              fail "GET /api/lookup/$lookup_id did not respond successfully"
              lookup_state=error
            fi
          done
          ;;
      esac
    fi
    case "$lookup_state" in
      done) pass "lookup job completed (polls=$polls)" ;;
      failed) fail "lookup job reached failed state (polls=$polls)" ;;
      running) fail "lookup job timed out (polls=$polls)" ;;
      error) : ;;
      *) fail "lookup job returned an invalid state" ;;
    esac
  fi
else
  fail "POST /api/lookup did not respond successfully"
fi

if [ "$hard_failures" -gt 0 ]; then
  printf 'RESULT FAIL hard_failures=%s warnings=%s\n' "$hard_failures" "$warnings"
  exit 1
fi
printf 'RESULT PASS warnings=%s\n' "$warnings"
exit 0
