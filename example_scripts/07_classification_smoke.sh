#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
TAB="${TAB:-UNCLASSIFIED}"
PET_ID="${PET_ID:-}"
AUTO_ACCEPT_THRESHOLD="${AUTO_ACCEPT_THRESHOLD:-0.78}"
CANDIDATE_THRESHOLD="${CANDIDATE_THRESHOLD:-0.62}"
SEARCH_LIMIT="${SEARCH_LIMIT:-200}"
TOP_K_IMAGES="${TOP_K_IMAGES:-50}"
PER_QUERY_LIMIT="${PER_QUERY_LIMIT:-400}"
DRY_RUN="${DRY_RUN:-true}"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/last_ingest.json}"

if [ -f "${STATE_FILE}" ]; then
  readarray -t AUTO_VALUES < <(python3 - <<'PY' "${STATE_FILE}"
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]
data = json.load(open(path, "r", encoding="utf-8"))

inst = data.get("instances") or []
instance_id = "ins_..."
for item in inst:
    if isinstance(item, dict) and item.get("instance_id"):
        instance_id = str(item["instance_id"])
        break

img = data.get("image") or {}
day = None
for k in ("captured_at", "uploaded_at"):
    v = img.get(k)
    if isinstance(v, str) and v:
        day = v[:10]
        break
if not day:
    day = datetime.now(timezone.utc).date().isoformat()

print(instance_id)
print(day)
PY
)
  INSTANCE_ID="${INSTANCE_ID:-${AUTO_VALUES[0]}}"
  DAY="${DAY:-${AUTO_VALUES[1]}}"
else
  INSTANCE_ID="${INSTANCE_ID:-ins_...}"
  DAY="${DAY:-$(date -u +%F)}"
fi

if [ "${INSTANCE_ID}" = "ins_..." ]; then
  echo "Usage: INSTANCE_ID=ins_uuid DAY=YYYY-MM-DD bash 07_classification_smoke.sh" >&2
  echo "Hint: run 03_ingest.sh first to auto-fill from ${STATE_FILE}" >&2
  exit 1
fi

echo "[1/4] auto classify"
cat > /tmp/classify_auto_body.json <<JSON
{
  "date": "${DAY}",
  "auto_accept_threshold": ${AUTO_ACCEPT_THRESHOLD},
  "candidate_threshold": ${CANDIDATE_THRESHOLD},
  "search_limit": ${SEARCH_LIMIT},
  "dry_run": ${DRY_RUN}
}
JSON
curl -sS -X POST "${API_BASE}/v1/classify/auto" \
  -H "Content-Type: application/json" \
  -d @/tmp/classify_auto_body.json | python3 -m json.tool

echo "[2/4] similar search in tab=${TAB}"
if [ "${TAB}" = "PET" ]; then
  if [ -z "${PET_ID}" ]; then
    echo "PET_ID is required when TAB=PET" >&2
    exit 1
  fi
  PET_FIELD=", \"pet_id\": \"${PET_ID}\""
else
  PET_FIELD=""
fi
cat > /tmp/classify_similar_body.json <<JSON
{
  "date": "${DAY}",
  "tab": "${TAB}"${PET_FIELD},
  "query_instance_ids": ["${INSTANCE_ID}"],
  "merge": "RRF",
  "top_k_images": ${TOP_K_IMAGES},
  "per_query_limit": ${PER_QUERY_LIMIT}
}
JSON
curl -sS -X POST "${API_BASE}/v1/classify/similar" \
  -H "Content-Type: application/json" \
  -d @/tmp/classify_similar_body.json | python3 -m json.tool

echo "[3/4] finalize buckets"
cat > /tmp/finalize_buckets_body.json <<JSON
{
  "date": "${DAY}"
}
JSON
FINALIZE_JSON="$(curl -sS -X POST "${API_BASE}/v1/buckets/finalize" \
  -H "Content-Type: application/json" \
  -d @/tmp/finalize_buckets_body.json)"
printf '%s\n' "${FINALIZE_JSON}" | python3 -m json.tool

MANIFEST_NAME="$(printf '%s\n' "${FINALIZE_JSON}" | python3 - <<'PY'
import json,sys,os
j=json.load(sys.stdin)
p=j.get("manifest_path") or ""
print(os.path.basename(p) if p else "")
PY
)"

echo "[4/4] read latest (or created) buckets"
if [ -n "${MANIFEST_NAME}" ]; then
  curl -sS "${API_BASE}/v1/buckets/${DAY}?manifest=${MANIFEST_NAME}" | python3 -m json.tool
else
  curl -sS "${API_BASE}/v1/buckets/${DAY}" | python3 -m json.tool
fi
