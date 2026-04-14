#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8001/v1}"
TODAY_UTC="${TODAY_UTC:-$(date -u +%F)}"
UPDATED_BY="${UPDATED_BY:-scenario_runner}"
RESET_FIRST="${RESET_FIRST:-true}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-pet_instances_v1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLEAR_QDRANT_SCRIPT="${CLEAR_QDRANT_SCRIPT:-${SCRIPT_DIR}/06_clear_qdrant.sh}"
SEED_ROOT="${SEED_ROOT:-${PROJECT_ROOT}/data/images_for_test/dc_001/registered}"
DAILY_ROOT="${DAILY_ROOT:-${PROJECT_ROOT}/data/images_for_test/dc_001/iphoneX/pictures/daily}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1"; exit 1; }
}

require_cmd curl
require_cmd python3

recreate_collection_if_needed() {
  local dim
  dim="$(curl -sS -m 3 "${BASE_URL}/health" | python3 -c 'import json,sys
raw=sys.stdin.read().strip()
try:
    d=json.loads(raw)
    print(int((d.get("model") or {}).get("dim") or 0))
except Exception:
    print(0)')"
  if [[ -z "${dim}" || "${dim}" == "0" ]]; then
    echo "[FAIL] could not resolve embedding dim from ${BASE_URL}/health"
    exit 1
  fi
  local body="{\"vectors\":{\"size\":${dim},\"distance\":\"Cosine\"}}"
  local code
  code="$(curl -sS -o /tmp/scenario11_recreate.out -w "%{http_code}" \
    -X PUT "${QDRANT_URL}/collections/${QDRANT_COLLECTION}" \
    -H "Content-Type: application/json" \
    -d "${body}" || true)"
  if [[ "${code}" == "200" || "${code}" == "201" || "${code}" == "202" ]]; then
    echo "[OK] Qdrant collection ready: ${QDRANT_COLLECTION} (dim=${dim})"
    return 0
  fi
  echo "[FAIL] recreate collection returned HTTP ${code}"
  cat /tmp/scenario11_recreate.out || true
  exit 1
}

if [[ "${RESET_FIRST}" == "true" ]]; then
  echo "[0/6] Reset storage outputs + Qdrant collection"
  if [[ ! -x "${CLEAR_QDRANT_SCRIPT}" ]]; then
    echo "clear script not executable: ${CLEAR_QDRANT_SCRIPT}"
    exit 1
  fi
  "${CLEAR_QDRANT_SCRIPT}" --hard
  recreate_collection_if_needed

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

json_post() {
  local url="$1"
  local body="$2"
  curl -sS -X POST "$url" -H "Content-Type: application/json" -d "$body"
}

safe_json_print() {
  local label="$1"
  local payload="$2"
  PAYLOAD="$payload" LABEL="$label" python3 - <<'PY'
import json
import os
import sys

label = os.environ.get("LABEL", "json")
raw = os.environ.get("PAYLOAD", "")
if not raw.strip():
    print(f"[FAIL] Empty response for {label}")
    sys.exit(1)
try:
    obj = json.loads(raw)
except Exception as e:
    print(f"[FAIL] Invalid JSON for {label}: {e}")
    print(raw)
    sys.exit(1)
print(json.dumps(obj, ensure_ascii=False))
PY
}

echo "[1/6] Seed folder upload"
if [[ ! -d "$SEED_ROOT" ]]; then
  echo "SEED_ROOT not found: $SEED_ROOT"
  echo "Set SEED_ROOT to a folder with structure root/<pet_name>/*.jpg"
  exit 1
fi

mapfile -t seed_files < <(find "$SEED_ROOT" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" \) | sort)
if [[ ${#seed_files[@]} -eq 0 ]]; then
  echo "No seed images found under $SEED_ROOT"
  exit 1
fi

form_args=(
  -F "updated_by=$UPDATED_BY"
  -F "sync_label=true"
  -F "apply_to_all_instances=false"
  -F "skip_on_error=true"
)

for f in "${seed_files[@]}"; do
  rel="${f#${SEED_ROOT%/}/}"
  # Keep root folder in relative path so server can parse root/pet/image pattern
  rel_with_root="$(basename "$SEED_ROOT")/$rel"
  form_args+=( -F "files=@$f" )
  form_args+=( -F "relative_paths=$rel_with_root" )
done

seed_resp="$(curl -sS -X POST "$BASE_URL/exemplars/upload-folder" "${form_args[@]}")"
echo "$seed_resp" | python3 -m json.tool >/tmp/seed_upload.json
seed_ok_count="$(python3 - <<'PY'
import json
with open('/tmp/seed_upload.json','r',encoding='utf-8') as f:
    d=json.load(f)
print(d.get('succeeded',0))
PY
)"
seed_fail_count="$(python3 - <<'PY'
import json
with open('/tmp/seed_upload.json','r',encoding='utf-8') as f:
    d=json.load(f)
print(d.get('failed',0))
PY
)"
python3 - <<'PY'
import json
with open('/tmp/seed_upload.json','r',encoding='utf-8') as f:
    d=json.load(f)
ids=[r.get('image_id') for r in d.get('results',[]) if r.get('status')=='ok' and r.get('image_id')]
with open('/tmp/seed_image_ids.json','w',encoding='utf-8') as o:
    json.dump(ids,o)
PY

echo "seed upload: succeeded=$seed_ok_count failed=$seed_fail_count"


echo "[2/6] Daily ingest from unlabeled folders (captured_at=today UTC)"
if [[ ! -d "$DAILY_ROOT" ]]; then
  echo "DAILY_ROOT not found: $DAILY_ROOT"
  exit 1
fi

mapfile -t daily_files < <(find "$DAILY_ROOT" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.webp" \) | sort)
if [[ ${#daily_files[@]} -eq 0 ]]; then
  echo "No daily images found under $DAILY_ROOT"
  exit 1
fi

ingested=0
for f in "${daily_files[@]}"; do
  # Spread timestamps by seconds to avoid identical captured_at if needed.
  ts="${TODAY_UTC}T09:00:00Z"
  curl -sS -X POST "$BASE_URL/ingest" \
    -F "file=@$f" \
    -F "trainer_id=$UPDATED_BY" \
    -F "captured_at=$ts" \
    -F "image_role=DAILY" \
    >/tmp/ingest_one.json
  ingested=$((ingested + 1))
done

echo "daily ingested: $ingested"


echo "[3/6] Auto classify"
auto_body="{\"date\":\"$TODAY_UTC\",\"auto_accept_threshold\":0.5,\"candidate_threshold\":0.4,\"search_limit\":200,\"dry_run\":false,\"labeled_by\":\"$UPDATED_BY\"}"
auto_resp="$(json_post "$BASE_URL/classify/auto" "$auto_body")"
echo "$auto_resp" | python3 -m json.tool >/tmp/auto_classify.json
python3 - <<'PY'
import json
with open('/tmp/auto_classify.json','r',encoding='utf-8') as f:
    d=json.load(f)
print(json.dumps(d.get('summary',{}), ensure_ascii=False, indent=2))
PY


echo "[4/6] Verify tabs (seed images should be excluded by default)"
unclassified_resp="$(curl -sS "$BASE_URL/images?date=$TODAY_UTC&tab=UNCLASSIFIED&limit=200")"
UNCLASSIFIED_RESP="$unclassified_resp" python3 - <<'PY'
import json, os
d=json.loads(os.environ['UNCLASSIFIED_RESP'])
print(json.dumps({"tab":"UNCLASSIFIED","count":d.get("count",0)}, ensure_ascii=False))
PY

pets_resp="$(curl -sS "$BASE_URL/pets")"
PETS_RESP="$pets_resp" python3 - <<'PY'
import json, os
d=json.loads(os.environ['PETS_RESP'])
print(json.dumps({"pets_count":d.get("count",0),"pets":[x.get("pet_id") for x in d.get("items",[])]}, ensure_ascii=False))
PY

mapfile -t pet_ids < <(PETS_RESP="$pets_resp" python3 - <<'PY'
import json, os
d=json.loads(os.environ['PETS_RESP'])
for x in d.get("items",[]):
    pid=x.get("pet_id")
    if pid:
        print(pid)
PY
)
for p in "${pet_ids[@]}"; do
  pet_tab_resp="$(curl -sS -G "$BASE_URL/images" \
    --data-urlencode "date=$TODAY_UTC" \
    --data-urlencode "tab=PET" \
    --data-urlencode "pet_id=$p" \
    --data-urlencode "limit=200")"
  safe_json_print "PET tab pet_id=$p raw response" "$pet_tab_resp" >/dev/null
  PET_TAB_RESP="$pet_tab_resp" PET_ID="$p" python3 - <<'PY'
import json, os
d=json.loads(os.environ['PET_TAB_RESP'])
print(json.dumps({"tab":"PET","pet_id":os.environ['PET_ID'],"count":d.get("count",0)}, ensure_ascii=False))
PY
done


echo "[5/6] Finalize buckets + ensure seed image_ids are not included"
finalize_body="{\"date\":\"$TODAY_UTC\"}"
finalize_resp="$(json_post "$BASE_URL/buckets/finalize" "$finalize_body")"
echo "$finalize_resp" | python3 -m json.tool >/tmp/finalize.json
python3 - <<'PY'
import json
with open('/tmp/finalize.json','r',encoding='utf-8') as f:
    d=json.load(f)
out={
    "bucket_count": d.get("bucket_count",0),
    "total_images": d.get("total_images",0),
    "quality_metrics": d.get("quality_metrics",{}),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
PY

seed_in_bucket_count="$(python3 - <<'PY'
import json
with open('/tmp/seed_image_ids.json','r',encoding='utf-8') as f:
    seed_ids=set(json.load(f))
with open('/tmp/finalize.json','r',encoding='utf-8') as f:
    fin=json.load(f)
bucket_ids=[]
for b in fin.get('buckets',[]) or []:
    bucket_ids.extend(b.get('image_ids',[]) or [])
print(sum(1 for x in bucket_ids if x in seed_ids))
PY
)"

if [[ "$seed_in_bucket_count" != "0" ]]; then
  echo "[FAIL] seed images found in finalized buckets: $seed_in_bucket_count"
  exit 1
fi

echo "[6/6] [PASS] Scenario complete. Seed images are excluded from daily tabs/buckets."
