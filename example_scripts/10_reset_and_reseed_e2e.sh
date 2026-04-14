#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-pet_instances_v1}"

DATA_TEST_ROOT="${DATA_TEST_ROOT:-${PROJECT_ROOT}/data/images_for_test}"
DAYCARE_ID="${DAYCARE_ID:-}"
DAY="${DAY:-}"
STORAGE_DIR="${STORAGE_DIR:-${PROJECT_ROOT}/data}"
REID_DIR="${REID_DIR:-${STORAGE_DIR}/reid}"
VERIFICATION_DIR="${VERIFICATION_DIR:-${STORAGE_DIR}/verification}"

# 0: delete points only (no API restart needed), 1: delete collection (may require API restart)
HARD_COLLECTION_RESET="${HARD_COLLECTION_RESET:-0}"
VERIFY_AFTER="${VERIFY_AFTER:-1}"
FORCE="${FORCE:-0}"
QDRANT_VECTOR_DIM="${QDRANT_VECTOR_DIM:-}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd python3

pick_first_dir_name() {
  local base="$1"
  find "$base" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort | head -n 1
}

pick_first_day_name() {
  local base="$1"
  find "$base" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' \
    | sort \
    | tail -n 1
}

if [[ -z "$DAYCARE_ID" ]]; then
  DAYCARE_ID="$(pick_first_dir_name "$DATA_TEST_ROOT")"
fi

if [[ -z "$DAYCARE_ID" ]]; then
  echo "Cannot auto-detect DAYCARE_ID under: ${DATA_TEST_ROOT}" >&2
  echo "Set DAYCARE_ID explicitly." >&2
  exit 1
fi

if [[ -z "$DAY" ]]; then
  DAY="$(pick_first_day_name "${DATA_TEST_ROOT}/${DAYCARE_ID}")"
fi

if [[ -z "$DAY" ]]; then
  echo "Cannot auto-detect DAY under: ${DATA_TEST_ROOT}/${DAYCARE_ID}" >&2
  echo "Expected folder like YYYY-MM-DD (e.g. 2026-02-13). Set DAY explicitly." >&2
  exit 1
fi

if [[ "$FORCE" != "1" ]]; then
  echo "[WARN] This will reset test state:"
  echo "  - Qdrant collection/points: ${QDRANT_COLLECTION}"
  echo "  - Local storage dirs:"
  echo "      ${REID_DIR}/{images,thumbs,meta,buckets}"
  echo "      ${VERIFICATION_DIR}/{pets,trials}"
  read -r -p "Proceed? (y/N): " ans
  if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

echo "== RESET+RESEED START =="
echo "API_BASE=${API_BASE}"
echo "QDRANT_URL=${QDRANT_URL}"
echo "QDRANT_COLLECTION=${QDRANT_COLLECTION}"
echo "DATA_TEST_ROOT=${DATA_TEST_ROOT}"
echo "DAYCARE_ID=${DAYCARE_ID}"
echo "DAY=${DAY}"
echo "STORAGE_DIR=${STORAGE_DIR}"
echo "REID_DIR=${REID_DIR}"
echo "VERIFICATION_DIR=${VERIFICATION_DIR}"
echo "HARD_COLLECTION_RESET=${HARD_COLLECTION_RESET}"
echo "QDRANT_VECTOR_DIM=${QDRANT_VECTOR_DIM:-auto}"

echo
echo "[1/5] reset qdrant"
if [[ "$HARD_COLLECTION_RESET" == "1" ]]; then
  code="$(curl -sS -o /tmp/qdrant_reset_resp.json -w "%{http_code}" -X DELETE "${QDRANT_URL}/collections/${QDRANT_COLLECTION}")" || true
  if [[ "$code" == "200" || "$code" == "202" || "$code" == "404" ]]; then
    echo "  - collection delete status: $code"
  else
    echo "Failed to delete collection (HTTP $code)" >&2
    cat /tmp/qdrant_reset_resp.json >&2 || true
    exit 1
  fi
else
  code="$(curl -sS -o /tmp/qdrant_reset_resp.json -w "%{http_code}" \
    -X POST "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/points/delete" \
    -H 'Content-Type: application/json' \
    -d '{"filter":{"must":[]},"wait":true}')" || true
  if [[ "$code" == "200" || "$code" == "202" ]]; then
    echo "  - points delete status: $code"
  elif [[ "$code" == "404" ]]; then
    echo "  - collection not found (404), continue"
  else
    echo "Failed to clear points (HTTP $code)" >&2
    cat /tmp/qdrant_reset_resp.json >&2 || true
    exit 1
  fi
fi

echo
echo "[2/5] ensure qdrant collection exists"
if [[ -z "$QDRANT_VECTOR_DIM" ]]; then
  QDRANT_VECTOR_DIM="$(curl -sS "${API_BASE}/v1/health" | python3 -c 'import sys,json; 
try:
 d=json.load(sys.stdin); print((d.get("model") or {}).get("dim") or "")
except Exception:
 print("")')"
fi
if [[ -z "$QDRANT_VECTOR_DIM" ]]; then
  echo "Cannot resolve vector dim from /v1/health. Set QDRANT_VECTOR_DIM explicitly." >&2
  exit 1
fi

ccode="$(curl -sS -o /tmp/qdrant_collection_check.json -w "%{http_code}" "${QDRANT_URL}/collections/${QDRANT_COLLECTION}")" || true
if [[ "$ccode" == "200" ]]; then
  echo "  - collection exists"
else
  echo "  - collection missing, creating (dim=${QDRANT_VECTOR_DIM})"
  make_code="$(curl -sS -o /tmp/qdrant_collection_make.json -w "%{http_code}" \
    -X PUT "${QDRANT_URL}/collections/${QDRANT_COLLECTION}" \
    -H 'Content-Type: application/json' \
    -d "{\"vectors\":{\"size\":${QDRANT_VECTOR_DIM},\"distance\":\"Cosine\"}}")" || true
  if [[ "$make_code" == "200" || "$make_code" == "201" || "$make_code" == "202" ]]; then
    echo "  - create status: $make_code"
  else
    echo "Failed to create collection (HTTP $make_code)" >&2
    cat /tmp/qdrant_collection_make.json >&2 || true
    exit 1
  fi
fi

echo
echo "[3/5] reset local storage"
rm -rf \
  "${REID_DIR}/images" \
  "${REID_DIR}/thumbs" \
  "${REID_DIR}/meta" \
  "${REID_DIR}/buckets" \
  "${VERIFICATION_DIR}/pets" \
  "${VERIFICATION_DIR}/trials"
mkdir -p \
  "${REID_DIR}/images" \
  "${REID_DIR}/thumbs" \
  "${REID_DIR}/meta" \
  "${REID_DIR}/buckets" \
  "${VERIFICATION_DIR}/pets" \
  "${VERIFICATION_DIR}/trials"
echo "  - local storage reset complete"

echo
echo "[4/5] run e2e reseed (08)"
API_BASE="$API_BASE" DAYCARE_ID="$DAYCARE_ID" DAY="$DAY" \
  bash "${SCRIPT_DIR}/08_e2e_registered_unlabeled.sh"

if [[ "$VERIFY_AFTER" == "1" ]]; then
  echo
  echo "[5/5] verify (09)"
  API_BASE="$API_BASE" DAYCARE_ID="$DAYCARE_ID" DAY="$DAY" \
    bash "${SCRIPT_DIR}/09_verify_after_e2e.sh"
else
  echo
  echo "[5/5] verify skipped (VERIFY_AFTER=${VERIFY_AFTER})"
fi

echo
echo "== RESET+RESEED DONE =="
