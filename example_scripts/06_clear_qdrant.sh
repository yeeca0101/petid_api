#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-pet_instances_v1}"
REID_ROOT="${REID_ROOT:-${PROJECT_ROOT}/data/reid}"
VERIFICATION_ROOT="${VERIFICATION_ROOT:-${PROJECT_ROOT}/data/verification}"
HAS_JQ=0
HARD_DELETE=0
CLEAR_STORAGE=0

if command -v jq >/dev/null 2>&1; then
  HAS_JQ=1
fi

for arg in "$@"; do
  case "$arg" in
    --hard)
      HARD_DELETE=1
      ;;
    --storage)
      CLEAR_STORAGE=1
      ;;
    -h|--help)
      cat <<USAGE
Usage: $(basename "$0") [--hard] [--storage]

Options:
  --hard      Delete the Qdrant collection itself.
  --storage   Also clear local storage outputs after DB cleanup.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

print_json() {
  local body="$1"
  if [[ "${HAS_JQ}" -eq 1 ]]; then
    echo "${body}" | jq .
  else
    echo "${body}"
  fi
}

is_qdrant_ok() {
  local body="$1"
  python3 - "$body" <<'PY'
import json
import sys

raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    print("invalid_json")
    sys.exit(2)

status = data.get("status")
if isinstance(status, dict) and status.get("error"):
    print(status.get("error"))
    sys.exit(1)

if isinstance(status, str) and status.lower() in {"ok", "acknowledged"}:
    print("ok")
    sys.exit(0)

result = data.get("result")
if isinstance(result, bool):
    print("ok" if result else "result_false")
    sys.exit(0 if result else 1)

print("ok")
sys.exit(0)
PY
}

clear_storage_outputs() {
  echo "[WARN] Clearing local storage outputs"
  rm -rf \
    "${REID_ROOT}/meta" \
    "${REID_ROOT}/images" \
    "${REID_ROOT}/thumbs" \
    "${REID_ROOT}/buckets" \
    "${VERIFICATION_ROOT}/pets" \
    "${VERIFICATION_ROOT}/trials"
  rm -f "${REID_ROOT}/registry/pets.json"

  mkdir -p \
    "${REID_ROOT}/meta" \
    "${REID_ROOT}/images" \
    "${REID_ROOT}/thumbs" \
    "${REID_ROOT}/buckets" \
    "${VERIFICATION_ROOT}/pets" \
    "${VERIFICATION_ROOT}/trials" \
    "${REID_ROOT}/registry"

  echo "[OK] Local storage outputs cleared."
}

if [[ "${HARD_DELETE}" -eq 1 ]]; then
  echo "[WARN] Deleting collection: ${QDRANT_COLLECTION}"
  resp="$(curl -sS -X DELETE "${QDRANT_URL}/collections/${QDRANT_COLLECTION}" || true)"
  print_json "${resp}"
  if msg="$(is_qdrant_ok "${resp}" 2>/dev/null)"; then
    echo "[OK] Collection deleted. It will be recreated on next API start."
    if [[ "${CLEAR_STORAGE}" -eq 1 ]]; then
      clear_storage_outputs
    fi
    exit 0
  fi
  echo "[FAIL] Collection delete failed: ${msg}"
  exit 1
fi

echo "[INFO] Soft clear: deleting all points in collection '${QDRANT_COLLECTION}'"
if [[ "${CLEAR_STORAGE}" -eq 1 ]]; then
  echo "[INFO] --storage enabled: local storage outputs will also be cleared."
fi
read -r -p "Proceed? (y/N): " ans
if [[ "${ans}" != "y" && "${ans}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

resp="$(curl -sS -X POST "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/points/delete" \
  -H 'Content-Type: application/json' \
  -d '{"filter": {"must": []}, "wait": true}' || true)"
print_json "${resp}"
if msg="$(is_qdrant_ok "${resp}" 2>/dev/null)"; then
  echo "[OK] All points deleted."
  if [[ "${CLEAR_STORAGE}" -eq 1 ]]; then
    clear_storage_outputs
  fi
  exit 0
fi

echo "[FAIL] Point delete failed: ${msg}"
exit 1
