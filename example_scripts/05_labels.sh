#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
INSTANCE_ID="${INSTANCE_ID:-ins_...}"
PET_ID="${PET_ID:-pet_aaa}"
SOURCE="${SOURCE:-MANUAL}"
CONFIDENCE="${CONFIDENCE:-1.0}"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/last_ingest.json}"

if [ "${INSTANCE_ID}" = "ins_..." ] && [ -f "${STATE_FILE}" ]; then
  INSTANCE_ID="$(python3 - <<'PY' "${STATE_FILE}"
import json
import sys
path = sys.argv[1]
data = json.load(open(path, "r", encoding="utf-8"))
inst = data.get("instances") or []
ids = [i.get("instance_id") for i in inst if isinstance(i, dict) and i.get("instance_id")]
print(ids[0] if ids else "ins_...")
PY
)"
fi

if [ "${INSTANCE_ID}" = "ins_..." ]; then
  echo "Usage: INSTANCE_ID=ins_uuid $0" >&2
  echo "Hint: run 03_ingest.sh first to auto-fill from ${STATE_FILE}" >&2
  exit 1
fi

cat > /tmp/labels_body.json <<JSON
{
  "assignments": [
    {"instance_id": "${INSTANCE_ID}", "pet_id": "${PET_ID}", "source": "${SOURCE}", "confidence": ${CONFIDENCE}}
  ]
}
JSON

curl -sS -X POST "${API_BASE}/v1/labels" \
  -H "Content-Type: application/json" \
  -d @/tmp/labels_body.json | cat
