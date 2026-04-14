#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_BASE="${API_BASE:-http://localhost:8001}"
DAY="${DAY:-2026-02-13}"
TRAINER_ID="${TRAINER_ID:-trainer_e2e}"
LABELED_BY="${LABELED_BY:-trainer_e2e}"
INCLUDE_EMB="${INCLUDE_EMB:-false}"
AUTO_ACCEPT_THRESHOLD="${AUTO_ACCEPT_THRESHOLD:-0.78}"
CANDIDATE_THRESHOLD="${CANDIDATE_THRESHOLD:-0.62}"
SEARCH_LIMIT="${SEARCH_LIMIT:-200}"

BASE_TEST_DIR="${BASE_TEST_DIR:-${PROJECT_ROOT}/data/images_for_test/dc_001}"
REGISTERED_DIR="${REGISTERED_DIR:-${BASE_TEST_DIR}/registered}"
UNLABELED_DIR="${UNLABELED_DIR:-${BASE_TEST_DIR}/${DAY}/unlabeled}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd python3

if [ ! -d "$REGISTERED_DIR" ]; then
  echo "REGISTERED_DIR not found: $REGISTERED_DIR" >&2
  exit 1
fi
if [ ! -d "$UNLABELED_DIR" ]; then
  echo "UNLABELED_DIR not found: $UNLABELED_DIR" >&2
  exit 1
fi

post_json() {
  local path="$1"
  local json_file="$2"
  local out_file="$3"
  local code
  code="$(curl -sS -o "$out_file" -w "%{http_code}" -X POST "${API_BASE}${path}" -H "Content-Type: application/json" -d @"$json_file")"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    echo "POST ${path} failed: HTTP $code" >&2
    cat "$out_file" >&2
    return 1
  fi
}

post_ingest() {
  local img="$1"
  local captured_at="$2"
  local out_file="$3"
  local code
  code="$(curl -sS -o "$out_file" -w "%{http_code}" -X POST "${API_BASE}/v1/ingest?include_embedding=${INCLUDE_EMB}" \
    -F "file=@${img}" \
    -F "trainer_id=${TRAINER_ID}" \
    -F "captured_at=${captured_at}")"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    echo "POST /v1/ingest failed: HTTP $code | img=$img" >&2
    cat "$out_file" >&2
    return 1
  fi
}

make_label_body() {
  local instance_id="$1"
  local pet_id="$2"
  local out_file="$3"
  python3 - <<'PY' "$LABELED_BY" "$instance_id" "$pet_id" "$out_file"
import json,sys

labeled_by,instance_id,pet_id,out_file=sys.argv[1:]
payload={
  "labeled_by":labeled_by,
  "assignments":[{
    "instance_id":instance_id,
    "action":"ACCEPT",
    "pet_id":pet_id,
    "source":"MANUAL",
    "confidence":1.0
  }]
}
with open(out_file,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False)
PY
}

make_auto_body() {
  local out_file="$1"
  python3 - <<'PY' "$DAY" "$AUTO_ACCEPT_THRESHOLD" "$CANDIDATE_THRESHOLD" "$SEARCH_LIMIT" "$out_file"
import json,sys

day,aat,ct,limit,out_file=sys.argv[1:]
payload={
  "date":day,
  "auto_accept_threshold":float(aat),
  "candidate_threshold":float(ct),
  "search_limit":int(limit),
  "dry_run":False
}
with open(out_file,"w",encoding="utf-8") as f:
  json.dump(payload,f,ensure_ascii=False)
PY
}

make_finalize_body() {
  local out_file="$1"
  python3 - <<'PY' "$DAY" "$out_file"
import json,sys

day,out_file=sys.argv[1:]
with open(out_file,"w",encoding="utf-8") as f:
  json.dump({"date":day},f,ensure_ascii=False)
PY
}

extract_first_instance_id() {
  local json_file="$1"
  python3 - <<'PY' "$json_file"
import json,sys
p=sys.argv[1]
try:
  d=json.load(open(p,"r",encoding="utf-8"))
except Exception:
  print("")
  raise SystemExit(0)
for inst in d.get("instances") or []:
  iid=inst.get("instance_id")
  if iid:
    print(iid)
    raise SystemExit(0)
print("")
PY
}

split_pet_folder() {
  local folder_name="$1"
  # Convention: {pet_id}__{pet_name}
  if [[ "$folder_name" == *"__"* ]]; then
    PET_ID_PARSED="${folder_name%%__*}"
    PET_NAME_PARSED="${folder_name#*__}"
  else
    PET_ID_PARSED="$folder_name"
    PET_NAME_PARSED="$folder_name"
  fi
  if [ -z "$PET_ID_PARSED" ]; then
    PET_ID_PARSED="$folder_name"
  fi
  if [ -z "$PET_NAME_PARSED" ]; then
    PET_NAME_PARSED="$PET_ID_PARSED"
  fi
}

echo "== E2E START =="
echo "API_BASE=$API_BASE"
echo "DAY=$DAY"
echo "REGISTERED_DIR=$REGISTERED_DIR"
echo "UNLABELED_DIR=$UNLABELED_DIR"

echo
echo "[1/5] ingest registered + label"
reg_ingested=0
reg_labeled=0
reg_skipped_no_image=0
reg_skipped_no_instance=0

while IFS= read -r -d '' d; do
  folder_name="$(basename "$d")"
  split_pet_folder "$folder_name"
  pet_id="$PET_ID_PARSED"
  pet_name="$PET_NAME_PARSED"

  first_img="$(find "$d" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) | sort | head -n 1 || true)"
  if [ -z "$first_img" ]; then
    echo "  - $folder_name -> $pet_id/$pet_name: skip (no image)"
    reg_skipped_no_image=$((reg_skipped_no_image+1))
    continue
  fi

  ingest_out="$TMP_DIR/reg_ingest_${pet_id}.json"
  captured_at="${DAY}T09:00:00Z"
  post_ingest "$first_img" "$captured_at" "$ingest_out"
  reg_ingested=$((reg_ingested+1))

  instance_id="$(extract_first_instance_id "$ingest_out")"
  if [ -z "$instance_id" ]; then
    echo "  - $folder_name -> $pet_id/$pet_name: ingest ok, but no detected instance"
    reg_skipped_no_instance=$((reg_skipped_no_instance+1))
    continue
  fi

  label_body="$TMP_DIR/label_${pet_id}.json"
  label_out="$TMP_DIR/label_${pet_id}_resp.json"
  make_label_body "$instance_id" "$pet_id" "$label_body"
  post_json "/v1/labels" "$label_body" "$label_out"
  reg_labeled=$((reg_labeled+1))
  echo "  - $folder_name -> $pet_id/$pet_name: labeled instance=$instance_id"
done < <(find "$REGISTERED_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

echo "registered summary: ingested=$reg_ingested labeled=$reg_labeled no_image=$reg_skipped_no_image no_instance=$reg_skipped_no_instance"

echo
echo "[2/5] ingest unlabeled"
unl_ingested=0
while IFS= read -r -d '' img; do
  sec=$((unl_ingested % 60))
  captured_at="${DAY}T10:00:$(printf '%02d' "$sec")Z"
  out="$TMP_DIR/unl_ingest_${unl_ingested}.json"
  post_ingest "$img" "$captured_at" "$out"
  unl_ingested=$((unl_ingested+1))
done < <(find "$UNLABELED_DIR" -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) -print0 | sort -z)

echo "unlabeled summary: ingested=$unl_ingested"

echo
echo "[3/5] auto classify (dry_run=false)"
auto_body="$TMP_DIR/auto_body.json"
auto_out="$TMP_DIR/auto_resp.json"
make_auto_body "$auto_body"
post_json "/v1/classify/auto" "$auto_body" "$auto_out"
python3 -m json.tool "$auto_out"

echo
echo "[4/5] finalize buckets"
fin_body="$TMP_DIR/finalize_body.json"
fin_out="$TMP_DIR/finalize_resp.json"
make_finalize_body "$fin_body"
post_json "/v1/buckets/finalize" "$fin_body" "$fin_out"
python3 -m json.tool "$fin_out"

manifest_name="$(python3 - <<'PY' "$fin_out"
import json,sys,os
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
p=j.get("manifest_path") or ""
print(os.path.basename(p) if p else "")
PY
)"

echo
echo "[5/5] get buckets"
if [ -n "$manifest_name" ]; then
  curl -sS "${API_BASE}/v1/buckets/${DAYCARE_ID}/${DAY}?manifest=${manifest_name}" | python3 -m json.tool
else
  curl -sS "${API_BASE}/v1/buckets/${DAYCARE_ID}/${DAY}" | python3 -m json.tool
fi

echo
echo "== E2E DONE =="
