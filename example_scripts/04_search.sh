#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
QUERY_INSTANCE_IDS_JSON="${QUERY_INSTANCE_IDS_JSON:-[\"ins_...\"]}"
MERGE="${MERGE:-RRF}"
SPECIES="${SPECIES:-DOG}"
TOP_K_IMAGES="${TOP_K_IMAGES:-200}"
PER_QUERY_LIMIT="${PER_QUERY_LIMIT:-400}"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/last_ingest.json}"

if [ "${QUERY_INSTANCE_IDS_JSON}" = "[\"ins_...\"]" ] && [ -f "${STATE_FILE}" ]; then
  QUERY_INSTANCE_IDS_JSON="$(python3 - <<'PY' "${STATE_FILE}"
import json
import sys
path = sys.argv[1]
data = json.load(open(path, "r", encoding="utf-8"))
inst = data.get("instances") or []
ids = [i.get("instance_id") for i in inst if isinstance(i, dict) and i.get("instance_id")]
print(json.dumps(ids if ids else ["ins_..."]))
PY
)"
fi

if [ "${QUERY_INSTANCE_IDS_JSON}" = "[\"ins_...\"]" ]; then
  echo "Usage: QUERY_INSTANCE_IDS_JSON='[\"ins_uuid\"]' $0" >&2
  echo "Hint: run 03_ingest.sh first to auto-fill from ${STATE_FILE}" >&2
  exit 1
fi

cat > /tmp/search_body.json <<JSON
{
  "query": {"instance_ids": ${QUERY_INSTANCE_IDS_JSON}, "merge": "${MERGE}"},
  "filters": {"species": "${SPECIES}"},
  "top_k_images": ${TOP_K_IMAGES},
  "per_query_limit": ${PER_QUERY_LIMIT}
}
JSON

curl -sS -X POST "${API_BASE}/v1/search" \
  -H "Content-Type: application/json" \
  -d @/tmp/search_body.json | cat
