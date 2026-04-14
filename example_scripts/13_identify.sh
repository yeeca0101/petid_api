#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# https://dramatic-bargain-ireland-financing.trycloudflare.com
API_BASE="${API_BASE:-http://localhost:8001}"
IMG="${IMG:-}"
DEFAULT_IMG="${DEFAULT_IMG:-}"
CAPTURED_AT="${CAPTURED_AT:-}"
TOP_K="${TOP_K:-1}"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/last_identify.json}"

if [ -z "${DEFAULT_IMG}" ]; then
  DEFAULT_IMG="$(find "${PROJECT_ROOT}/data/images_for_test" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) 2>/dev/null | sort | head -n 1 || true)"
fi

if [ -z "${IMG}" ] && [ -f "${DEFAULT_IMG}" ]; then
  IMG="${DEFAULT_IMG}"
fi

if [ -z "${IMG}" ] || [ ! -f "${IMG}" ]; then
  echo "Usage: IMG=/path/to.jpg bash $0" >&2
  echo "Optional: CAPTURED_AT=2026-03-25T09:00:00+09:00 TOP_K=3" >&2
  echo "Hint: set IMG, or place a file at ${DEFAULT_IMG}" >&2
  exit 1
fi

tmp_body="$(mktemp)"
http_code="$(curl -sS -o "${tmp_body}" -w "%{http_code}" -X POST "${API_BASE}/v1/identify" \
  -F "file=@${IMG}" \
  -F "top_k=${TOP_K}" \
  $( [ -n "${CAPTURED_AT}" ] && echo "-F captured_at=${CAPTURED_AT}" ))"

cat "${tmp_body}"
cp "${tmp_body}" "${STATE_FILE}"

if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
  echo ""
  echo "Request failed: HTTP ${http_code}" >&2
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
except Exception as e:
    print(f"\nFailed to parse response: {e}")
    raise SystemExit(1)

print("\nIdentify summary:")
print(f"image_id   : {data.get('image_id')}")
print(f"instance_id: {data.get('instance_id')}")
print(f"species    : {data.get('species')}")

bbox = data.get("bbox") or {}
if bbox:
    print(
        "bbox       : "
        f"x1={bbox.get('x1')} y1={bbox.get('y1')} x2={bbox.get('x2')} y2={bbox.get('y2')}"
    )

candidates = data.get("candidates") or []
if not candidates:
    print("candidates : none")
else:
    print("candidates :")
    for idx, item in enumerate(candidates, start=1):
        pet_id = item.get("pet_id")
        pet_name = item.get("pet_name") or "-"
        score = item.get("score")
        print(f"  {idx}. pet_id={pet_id} pet_name={pet_name} score={score}")
PY
