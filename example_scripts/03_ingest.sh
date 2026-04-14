#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
IMG="${IMG:-}"
DEFAULT_IMG="${DEFAULT_IMG:-}"
TRAINER_ID="${TRAINER_ID:-}"
CAPTURED_AT="${CAPTURED_AT:-}"
INCLUDE_EMB="${INCLUDE_EMB:-true}"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/last_ingest.json}"

if [ -z "${DEFAULT_IMG}" ]; then
  DEFAULT_IMG="$(find "${PROJECT_ROOT}/data/images_for_test" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) 2>/dev/null | sort | head -n 1 || true)"
fi

if [ -z "${IMG}" ] && [ -f "${DEFAULT_IMG}" ]; then
  IMG="${DEFAULT_IMG}"
fi

if [ -z "${IMG}" ] || [ ! -f "${IMG}" ]; then
  echo "Usage: IMG=/path/to.jpg $0" >&2
  echo "Hint: set IMG, or place a file at ${DEFAULT_IMG}" >&2
  exit 1
fi

tmp_body="$(mktemp)"
http_code="$(curl -sS -o "${tmp_body}" -w "%{http_code}" -X POST "${API_BASE}/v1/ingest?include_embedding=${INCLUDE_EMB}" \
  -F "file=@${IMG}" \
  $( [ -n "${TRAINER_ID}" ] && echo "-F trainer_id=${TRAINER_ID}" ) \
  $( [ -n "${CAPTURED_AT}" ] && echo "-F captured_at=${CAPTURED_AT}" ))"

cat "${tmp_body}"
cp "${tmp_body}" "${STATE_FILE}"

if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
  echo ""
  echo "Request failed: HTTP ${http_code}"
  rm -f "${tmp_body}"
  exit 1
fi

rm -f "${tmp_body}"

python3 - <<'PY' "${STATE_FILE}"
import json
import sys
path = sys.argv[1]
try:
    data = json.load(open(path, "r", encoding="utf-8"))
    inst = data.get("instances") or []
    ids = [i.get("instance_id") for i in inst if isinstance(i, dict) and i.get("instance_id")]
    if ids:
        print("\nSaved instance_id(s):")
        for pid in ids:
            print(pid)
    else:
        print("\nNo instance_id found in response.")
except Exception as e:
    print(f"\nFailed to parse response: {e}")
PY
