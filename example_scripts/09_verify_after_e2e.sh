#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8001}"
DAY="${DAY:-2026-02-13}"
LIMIT="${LIMIT:-2000}"

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

get_json() {
  local path="$1"
  local out_file="$2"
  local code
  code="$(curl -sS -o "$out_file" -w "%{http_code}" "${API_BASE}${path}")"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    echo "GET ${path} failed: HTTP $code" >&2
    cat "$out_file" >&2
    return 1
  fi
}

echo "== VERIFY START =="
echo "API_BASE=$API_BASE"
echo "DAY=$DAY"

echo
echo "[1/5] health"
HEALTH_JSON="$TMP_DIR/health.json"
get_json "/v1/health" "$HEALTH_JSON"
python3 -m json.tool "$HEALTH_JSON"

echo
echo "[2/5] pets list"
PETS_JSON="$TMP_DIR/pets.json"
get_json "/v1/pets" "$PETS_JSON"
python3 -m json.tool "$PETS_JSON"

python3 - <<'PY' "$PETS_JSON"
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
items=j.get("items") or []
print("\n[pets summary]")
print(f"count={len(items)}")
for it in items:
    pid=it.get("pet_id")
    pname=it.get("pet_name") or pid
    ic=it.get("image_count",0)
    nc=it.get("instance_count",0)
    print(f"- {pname} ({pid}): images={ic}, instances={nc}")
PY

echo
echo "[3/5] images counts by tab"
ALL_JSON="$TMP_DIR/images_all.json"
UNL_JSON="$TMP_DIR/images_unl.json"
get_json "/v1/images?date=${DAY}&tab=ALL&limit=${LIMIT}&offset=0" "$ALL_JSON"
get_json "/v1/images?date=${DAY}&tab=UNCLASSIFIED&limit=${LIMIT}&offset=0" "$UNL_JSON"

python3 - <<'PY' "$ALL_JSON" "$UNL_JSON" "$PETS_JSON"
import json,sys,urllib.parse,urllib.request
allj=json.load(open(sys.argv[1],"r",encoding="utf-8"))
unlj=json.load(open(sys.argv[2],"r",encoding="utf-8"))
pets=json.load(open(sys.argv[3],"r",encoding="utf-8"))

print("[images summary]")
print(f"ALL.count={allj.get('count',0)}")
print(f"UNCLASSIFIED.count={unlj.get('count',0)}")

# PET tab counts
api_base = __import__('os').environ.get('API_BASE','http://localhost:8001').rstrip('/')
day = __import__('os').environ.get('DAY','2026-02-13')
limit = __import__('os').environ.get('LIMIT','2000')

for p in (pets.get("items") or []):
    pid=p.get("pet_id")
    if not pid:
        continue
    q=urllib.parse.urlencode({
        "date": day,
        "tab": "PET",
        "pet_id": pid,
        "limit": limit,
        "offset": 0,
    })
    url=f"{api_base}/v1/images?{q}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d=json.loads(r.read().decode("utf-8"))
            cnt=d.get("count",0)
    except Exception:
        cnt="ERR"
    pname=p.get("pet_name") or pid
    print(f"PET[{pname}/{pid}].count={cnt}")
PY

echo
echo "[4/5] buckets"
BUCKETS_JSON="$TMP_DIR/buckets.json"
if curl -sS -o "$BUCKETS_JSON" -w "%{http_code}" "${API_BASE}/v1/buckets/${DAY}" | grep -Eq '^[23]'; then
  python3 -m json.tool "$BUCKETS_JSON"
else
  echo "buckets not found yet (run finalize first)."
fi

echo
echo "[5/5] sample image meta (first image in ALL)"
python3 - <<'PY' "$ALL_JSON" "$API_BASE"
import json,sys,urllib.request
allj=json.load(open(sys.argv[1],"r",encoding="utf-8"))
api_base=sys.argv[2].rstrip('/')
items=allj.get('items') or []
if not items:
    print('no images in ALL tab for this day')
    raise SystemExit(0)
img_id=items[0].get('image_id')
if not img_id:
    print('first image has no image_id')
    raise SystemExit(0)
url=f"{api_base}/v1/images/{img_id}/meta"
with urllib.request.urlopen(url, timeout=30) as r:
    meta=json.loads(r.read().decode('utf-8'))
print(json.dumps({
  'image_id': img_id,
  'instance_count': len(meta.get('instances') or []),
  'sample_instances': (meta.get('instances') or [])[:3],
}, ensure_ascii=False, indent=2))
PY

echo
echo "== VERIFY DONE =="
