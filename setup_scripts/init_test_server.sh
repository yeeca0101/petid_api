#!/usr/bin/env bash
set -euo pipefail

# Initializes and validates server pipeline for a disposable daycare.
# - Optional reset of DB/storage
# - Seed upload from registered folder
# - Daily upload from iphoneX/pictures
# - Auto-classify (threshold=0.4)
# - Endpoint sanity checks
# - Always cleanup daycare data (DB + storage) on exit
#
# Usage:
#   bash init_server.sh [--reset-db] [--reset-storage] [--base-url http://localhost:8001/v1]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}"

BASE_URL="${BASE_URL:-http://localhost:8001/v1}"
DAYCARE_ID="${DAYCARE_ID:-dc_test01}"
SEED_ROOT="${SEED_ROOT:-${PROJECT_ROOT}/data/images_for_test/dc_001/registered}"
DAILY_ROOT="${DAILY_ROOT:-${PROJECT_ROOT}/data/images_for_test/dc_001/iphoneX/pictures}"
UPDATED_BY="${UPDATED_BY:-init_server}"
AUTO_TH="${AUTO_TH:-0.4}"
WAIT_API_SECONDS="${WAIT_API_SECONDS:-20}"
QDRANT_HEALTH_URL="${QDRANT_HEALTH_URL:-http://localhost:6333/collections}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-pet_instances_v1}"
RESET_DB=0
RESET_STORAGE=0
START_SERVICES=0
KEEP_SERVICES=0
QDRANT_PID=""
API_PID=""

usage() {
  cat <<EOF
Usage: bash init_server.sh [options]

Options:
  --reset-db               Reset Qdrant collection before scenario
  --reset-storage          Reset storage outputs (reid + verification) before scenario
  --base-url <url>         API base URL (default: ${BASE_URL})
  --wait-api-seconds <n>   Wait timeout for API health (default: ${WAIT_API_SECONDS})
  --start-services         Start run_qdrant.sh and run_api.sh in background
  --keep-services          Do not stop started services at script exit
  --qdrant-health-url <u>  Qdrant health URL (default: ${QDRANT_HEALTH_URL})
  --qdrant-url <u>         Qdrant base URL (default: ${QDRANT_URL})
  --qdrant-collection <c>  Qdrant collection name (default: ${QDRANT_COLLECTION})
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset-db)
      RESET_DB=1
      shift
      ;;
    --reset-storage)
      RESET_STORAGE=1
      shift
      ;;
    --base-url)
      BASE_URL="${2:-}"
      if [[ -z "${BASE_URL}" ]]; then
        echo "missing value for --base-url"
        exit 1
      fi
      shift 2
      ;;
    --wait-api-seconds)
      WAIT_API_SECONDS="${2:-}"
      if [[ -z "${WAIT_API_SECONDS}" ]]; then
        echo "missing value for --wait-api-seconds"
        exit 1
      fi
      shift 2
      ;;
    --start-services)
      START_SERVICES=1
      shift
      ;;
    --keep-services)
      KEEP_SERVICES=1
      shift
      ;;
    --qdrant-health-url)
      QDRANT_HEALTH_URL="${2:-}"
      if [[ -z "${QDRANT_HEALTH_URL}" ]]; then
        echo "missing value for --qdrant-health-url"
        exit 1
      fi
      shift 2
      ;;
    --qdrant-url)
      QDRANT_URL="${2:-}"
      if [[ -z "${QDRANT_URL}" ]]; then
        echo "missing value for --qdrant-url"
        exit 1
      fi
      shift 2
      ;;
    --qdrant-collection)
      QDRANT_COLLECTION="${2:-}"
      if [[ -z "${QDRANT_COLLECTION}" ]]; then
        echo "missing value for --qdrant-collection"
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1"; exit 1; }
}

require_cmd curl
require_cmd python3

json_post() {
  local path="$1"
  local body="$2"
  curl -sS -X POST "${BASE_URL}${path}" -H "Content-Type: application/json" -d "${body}"
}

api_get() {
  local path="$1"
  curl -sS "${BASE_URL}${path}"
}

wait_for_api() {
  local waited=0
  local step=2
  local health_url="${BASE_URL}/health"
  echo "[INFO] waiting for API: ${health_url} (timeout=${WAIT_API_SECONDS}s)"
  while [[ "${waited}" -lt "${WAIT_API_SECONDS}" ]]; do
    if curl -sS -m 2 "${health_url}" >/tmp/init_server_health.json 2>/tmp/init_server_health.err; then
      echo "[OK] API is reachable"
      return 0
    fi
    sleep "${step}"
    waited=$((waited + step))
  done
  echo "[FAIL] API not reachable: ${health_url}"
  echo "hint: start API first (e.g. ./run_api.sh), or set --base-url correctly."
  if [[ -s /tmp/init_server_health.err ]]; then
    echo "last error: $(cat /tmp/init_server_health.err)"
  fi
  return 1
}

wait_for_url() {
  local url="$1"
  local timeout="$2"
  local label="$3"
  local waited=0
  local step=2
  echo "[INFO] waiting for ${label}: ${url} (timeout=${timeout}s)"
  while [[ "${waited}" -lt "${timeout}" ]]; do
    if curl -sS -m 2 "${url}" >/tmp/init_server_wait.out 2>/tmp/init_server_wait.err; then
      echo "[OK] ${label} is reachable"
      return 0
    fi
    sleep "${step}"
    waited=$((waited + step))
  done
  echo "[FAIL] ${label} not reachable: ${url}"
  if [[ -s /tmp/init_server_wait.err ]]; then
    echo "last error: $(cat /tmp/init_server_wait.err)"
  fi
  return 1
}

start_services_if_requested() {
  if [[ "${START_SERVICES}" -ne 1 ]]; then
    return 0
  fi

  if [[ ! -f "${PROJECT_ROOT}/run_qdrant.sh" ]]; then
    echo "missing run_qdrant.sh"
    exit 1
  fi
  if [[ ! -f "${PROJECT_ROOT}/run_api.sh" ]]; then
    echo "missing run_api.sh"
    exit 1
  fi

  echo "[0.s1/9] start qdrant in background"
  bash "${PROJECT_ROOT}/run_qdrant.sh" >/tmp/init_server_qdrant.log 2>&1 &
  QDRANT_PID="$!"
  wait_for_url "${QDRANT_HEALTH_URL}" "${WAIT_API_SECONDS}" "Qdrant"

  echo "[0.s2/9] start api in background"
  bash "${PROJECT_ROOT}/run_api.sh" >/tmp/init_server_api.log 2>&1 &
  API_PID="$!"
  wait_for_api
}

recreate_collection_if_needed() {
  if [[ "${RESET_DB}" -ne 1 ]]; then
    return 0
  fi
  echo "[0.15/9] recreate qdrant collection if missing"
  local dim
  dim="$(curl -sS -m 3 "${BASE_URL}/health" | python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    d=json.loads(raw)
    print(int((d.get("model") or {}).get("dim") or 0))
except Exception:
    print(0)')"
  if [[ -z "${dim}" || "${dim}" == "0" ]]; then
    echo "[WARN] could not resolve embedding dim from /health, skip explicit recreate"
    return 0
  fi
  local body
  body="{\"vectors\":{\"size\":${dim},\"distance\":\"Cosine\"}}"
  local code
  code="$(curl -sS -o /tmp/init_server_recreate.out -w "%{http_code}" \
    -X PUT "${QDRANT_URL}/collections/${QDRANT_COLLECTION}" \
    -H "Content-Type: application/json" \
    -d "${body}" || true)"
  if [[ "${code}" == "200" || "${code}" == "201" ]]; then
    echo "[OK] collection ready: ${QDRANT_COLLECTION} (dim=${dim})"
  else
    echo "[WARN] recreate collection returned HTTP ${code}"
    cat /tmp/init_server_recreate.out || true
  fi
}

json_assert_ok() {
  local label="$1"
  local payload="$2"
  PAYLOAD="${payload}" LABEL="${label}" python3 - <<'PY'
import json
import os
import sys

label = os.environ["LABEL"]
raw = os.environ.get("PAYLOAD", "")
if not raw.strip():
    print(f"[FAIL] empty response: {label}")
    sys.exit(1)
try:
    json.loads(raw)
except Exception as e:
    print(f"[FAIL] invalid json: {label}: {e}")
    print(raw)
    sys.exit(1)
print(f"[OK] {label}")
PY
}

cleanup_daycare() {
  echo "[CLEANUP] removing daycare '${DAYCARE_ID}' (db+storage)"
  curl -sS -X DELETE \
    "${BASE_URL}/daycares/${DAYCARE_ID}?delete_qdrant=true&delete_storage=true" \
    >/tmp/init_server_cleanup.json 2>/tmp/init_server_cleanup.err || true
  if [[ -s /tmp/init_server_cleanup.json ]]; then
    cat /tmp/init_server_cleanup.json || true
  fi

  if [[ "${START_SERVICES}" -eq 1 && "${KEEP_SERVICES}" -ne 1 ]]; then
    echo "[CLEANUP] stopping started background services"
    if [[ -n "${API_PID}" ]] && kill -0 "${API_PID}" 2>/dev/null; then
      kill "${API_PID}" 2>/dev/null || true
      wait "${API_PID}" 2>/dev/null || true
    fi
    if [[ -n "${QDRANT_PID}" ]] && kill -0 "${QDRANT_PID}" 2>/dev/null; then
      kill "${QDRANT_PID}" 2>/dev/null || true
      wait "${QDRANT_PID}" 2>/dev/null || true
    fi
    echo "[CLEANUP] service logs:"
    echo "  qdrant: /tmp/init_server_qdrant.log"
    echo "  api:    /tmp/init_server_api.log"
  fi
}

trap cleanup_daycare EXIT

echo "[0/9] pre-checks"
if [[ ! -d "${SEED_ROOT}" ]]; then
  echo "SEED_ROOT not found: ${SEED_ROOT}"
  exit 1
fi
if [[ ! -d "${DAILY_ROOT}" ]]; then
  echo "DAILY_ROOT not found: ${DAILY_ROOT}"
  exit 1
fi
start_services_if_requested
if [[ "${START_SERVICES}" -ne 1 ]]; then
  wait_for_api
fi

if [[ "${RESET_DB}" -eq 1 ]]; then
  echo "[0.1/9] reset db"
  if [[ ! -x "${PROJECT_ROOT}/example_scripts/06_clear_qdrant.sh" ]]; then
    echo "missing clear script: ${PROJECT_ROOT}/example_scripts/06_clear_qdrant.sh"
    exit 1
  fi
  "${PROJECT_ROOT}/example_scripts/06_clear_qdrant.sh" --hard
fi
recreate_collection_if_needed

if [[ "${RESET_STORAGE}" -eq 1 ]]; then
  echo "[0.2/9] reset storage outputs"
  rm -rf \
    "${PROJECT_ROOT}/data/reid/meta" \
    "${PROJECT_ROOT}/data/reid/images" \
    "${PROJECT_ROOT}/data/reid/thumbs" \
    "${PROJECT_ROOT}/data/reid/buckets" \
    "${PROJECT_ROOT}/data/verification/pets" \
    "${PROJECT_ROOT}/data/verification/trials"
  mkdir -p \
    "${PROJECT_ROOT}/data/reid/meta" \
    "${PROJECT_ROOT}/data/reid/images" \
    "${PROJECT_ROOT}/data/reid/thumbs" \
    "${PROJECT_ROOT}/data/reid/buckets" \
    "${PROJECT_ROOT}/data/verification/pets" \
    "${PROJECT_ROOT}/data/verification/trials"
fi

echo "[1/9] health checks"
health="$(api_get "/health")"
json_assert_ok "GET /health" "${health}"
qhealth="$(api_get "/health/qdrant")"
json_assert_ok "GET /health/qdrant" "${qhealth}"

echo "[2/9] upload seed exemplars (folder)"
mapfile -t seed_files < <(find "${SEED_ROOT}" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" \) | sort)
if [[ ${#seed_files[@]} -eq 0 ]]; then
  echo "no seed images in ${SEED_ROOT}"
  exit 1
fi
form_seed=(
  -F "daycare_id=${DAYCARE_ID}"
  -F "updated_by=${UPDATED_BY}"
  -F "sync_label=true"
  -F "apply_to_all_instances=false"
  -F "skip_on_error=true"
)
for f in "${seed_files[@]}"; do
  rel="${f#${SEED_ROOT%/}/}"
  rel_with_root="$(basename "${SEED_ROOT}")/${rel}"
  form_seed+=( -F "files=@${f}" )
  form_seed+=( -F "relative_paths=${rel_with_root}" )
done
seed_resp="$(curl -sS -X POST "${BASE_URL}/exemplars/upload-folder" "${form_seed[@]}")"
json_assert_ok "POST /exemplars/upload-folder" "${seed_resp}"
SEED_RESP="${seed_resp}" python3 - <<'PY'
import json,os,sys
d=json.loads(os.environ["SEED_RESP"])
if int(d.get("succeeded",0)) <= 0:
    print("[FAIL] seed upload succeeded=0")
    print(json.dumps(d, ensure_ascii=False, indent=2))
    sys.exit(1)
print(f"[OK] seed upload: succeeded={d.get('succeeded')} failed={d.get('failed')}")
PY

echo "[3/9] upload daily images (ingest)"
mapfile -t daily_files < <(find "${DAILY_ROOT}" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" \) | sort)
if [[ ${#daily_files[@]} -eq 0 ]]; then
  echo "no daily images in ${DAILY_ROOT}"
  exit 1
fi
ingested=0
for f in "${daily_files[@]}"; do
  ts="$(date -u +%FT%TZ)"
  resp="$(curl -sS -X POST "${BASE_URL}/ingest" \
    -F "file=@${f}" \
    -F "daycare_id=${DAYCARE_ID}" \
    -F "trainer_id=${UPDATED_BY}" \
    -F "captured_at=${ts}" \
    -F "image_role=DAILY")"
  json_assert_ok "POST /ingest $(basename "${f}")" "${resp}" >/dev/null
  ingested=$((ingested + 1))
done
echo "[OK] daily ingested: ${ingested}"

echo "[4/9] endpoint sanity checks before classify"
dc_list="$(api_get "/daycares?limit=500")"
json_assert_ok "GET /daycares" "${dc_list}"
DC_LIST="${dc_list}" DAYCARE_ID="${DAYCARE_ID}" python3 - <<'PY'
import json, os, sys
d = json.loads(os.environ["DC_LIST"])
target = os.environ["DAYCARE_ID"]
ids = [x.get("daycare_id") for x in d.get("items",[])]
if target not in ids:
    print(f"[FAIL] daycare not found in /daycares: {target}")
    sys.exit(1)
print(f"[OK] daycare found in /daycares: {target}")
PY

ex_list="$(api_get "/exemplars?daycare_id=${DAYCARE_ID}&limit=500")"
json_assert_ok "GET /exemplars" "${ex_list}"

img_all="$(api_get "/images?daycare_id=${DAYCARE_ID}&tab=ALL&include_seed=false&limit=500")"
json_assert_ok "GET /images(tab=ALL)" "${img_all}"

pets_resp="$(api_get "/pets?daycare_id=${DAYCARE_ID}")"
json_assert_ok "GET /pets" "${pets_resp}"

echo "[5/9] auto classify (threshold=${AUTO_TH})"
today="$(date -u +%F)"
auto_body="{\"daycare_id\":\"${DAYCARE_ID}\",\"date\":\"${today}\",\"auto_accept_threshold\":${AUTO_TH},\"candidate_threshold\":0.3,\"search_limit\":200,\"dry_run\":false,\"labeled_by\":\"${UPDATED_BY}\"}"
auto_resp="$(json_post "/classify/auto" "${auto_body}")"
json_assert_ok "POST /classify/auto" "${auto_resp}"
AUTO_RESP="${auto_resp}" python3 - <<'PY'
import json, os
d=json.loads(os.environ["AUTO_RESP"])
print("[INFO] auto summary:", json.dumps(d.get("summary",{}), ensure_ascii=False))
PY

echo "[6/9] similar/search endpoint checks"
query_instance="$(EX_LIST="${ex_list}" python3 - <<'PY'
import json, os
d=json.loads(os.environ["EX_LIST"])
items=d.get("items") or []
print(items[0].get("instance_id") if items else "")
PY
)"
if [[ -n "${query_instance}" ]]; then
  similar_body="{\"daycare_id\":\"${DAYCARE_ID}\",\"date\":\"${today}\",\"tab\":\"ALL\",\"include_seed\":false,\"query_instance_ids\":[\"${query_instance}\"],\"merge\":\"RRF\",\"top_k_images\":50,\"per_query_limit\":100}"
  similar_resp="$(json_post "/classify/similar" "${similar_body}")"
  json_assert_ok "POST /classify/similar" "${similar_resp}"

  search_body="{\"daycare_id\":\"${DAYCARE_ID}\",\"query\":{\"instance_ids\":[\"${query_instance}\"],\"merge\":\"RRF\"},\"filters\":{},\"top_k_images\":50,\"per_query_limit\":100}"
  search_resp="$(json_post "/search" "${search_body}")"
  json_assert_ok "POST /search" "${search_resp}"
fi

echo "[7/9] finalize + get buckets"
fin_body="{\"daycare_id\":\"${DAYCARE_ID}\",\"date\":\"${today}\"}"
fin_resp="$(json_post "/buckets/finalize" "${fin_body}")"
json_assert_ok "POST /buckets/finalize" "${fin_resp}"
bucket_get="$(api_get "/buckets/${DAYCARE_ID}/${today}")"
json_assert_ok "GET /buckets/{daycare_id}/{day}" "${bucket_get}"

echo "[8/9] post-classify gallery checks"
img_unclassified="$(api_get "/images?daycare_id=${DAYCARE_ID}&date=${today}&tab=UNCLASSIFIED&limit=500")"
json_assert_ok "GET /images(tab=UNCLASSIFIED)" "${img_unclassified}"
img_all_seed="$(api_get "/images?daycare_id=${DAYCARE_ID}&date=${today}&tab=ALL&include_seed=true&limit=500")"
json_assert_ok "GET /images(include_seed=true)" "${img_all_seed}"

echo "[9/9] all tests passed (cleanup will run now)"
