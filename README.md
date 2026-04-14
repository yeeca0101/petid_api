# DogFace Semi-auto Classification API (FastAPI, GPU PoC)

PoC server for **pet instance detection + embedding + vector search** to support semi-automatic gallery sorting by dog/cat identity.

## What this server does
- Loads an embedding model (default: `miewid` / conservationxlabs/miewid-msv3)
- Loads a YOLO detector (YOLO26x weights path configured via env)
- On ingest: detects pets (Cat=15, Dog=16), crops each instance with padding, embeds each crop
- Stores per-instance vectors in **Qdrant** (docker-compose included)
- Exposes a search endpoint to return gallery ordering (image_id list) by similarity

> In PoC, keep the server **single-process** (one uvicorn worker) because the GPU model will be duplicated per process.

---

## Quickstart (Docker Compose, GPU + Qdrant)
### 0) Put YOLO weights
Copy/mount your YOLO26x weights to:
`./weights/yolo/yolo26x.pt` (or set `YOLO_WEIGHTS_PATH`)

### 1) Run
```bash
docker compose up --build
```

Open:
- Swagger UI: `http://<server-ip>:8000/docs`
- Health: `http://<server-ip>:8000/v1/health`

---

## Run with provided scripts (no docker-compose)
This repo also includes shell scripts used on the server setup.

1) Start Qdrant
```bash
./run_qdrant.sh
```

2) Start API (loads `.env` and runs on port 8001)
```bash
./run_api.sh
```

Open:
- Swagger UI: `http://<server-ip>:8001/docs`
- Health: `http://<server-ip>:8001/v1/health`

---

## Run locally (without Docker)
1) Install PyTorch with CUDA (example for CUDA 12.1)
```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```
2) Install the remaining deps
```bash
pip install -r requirements.txt
```
3) Start
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Endpoints
### 1) Health
`GET /v1/health`

### 2) Ingest (upload → detect → embed → store)
`POST /v1/ingest`
- multipart/form-data fields
  - `file` (image)
  - `trainer_id` (optional)
  - `captured_at` (optional ISO8601)

Example:
```bash
curl -X POST "http://localhost:8000/v1/ingest?include_embedding=false" \
  -F "file=@/path/to/photo.jpg"
```

### 3) Search (gallery ordering by similarity)
`POST /v1/search` (JSON)

```bash
curl -X POST "http://localhost:8000/v1/search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"instance_ids": ["ins_..."], "merge": "RRF"},
    "filters": {"species": "DOG"},
    "top_k_images": 200
  }'
```

### 4) Labels (instance_id → pet_id)
`POST /v1/labels` (JSON)

```bash
curl -X POST "http://localhost:8000/v1/labels" \
  -H "Content-Type: application/json" \
  -d '{
    "assignments": [{"instance_id": "ins_...", "pet_id": "pet_aaa", "source": "MANUAL"}]
  }'
```

### 5) Images (gallery)
`GET /v1/images`
- Query: `date`, `tab`, `pet_id`, `include_seed`, `limit`, `offset`

`GET /v1/images/{image_id}?variant=raw|thumb`

`GET /v1/images/{image_id}/meta`

### 6) (Legacy) Single embedding
`POST /v1/embed`
- `multipart/form-data` with field `file`
- Query: `format=json|f32|f16`

JSON response example:
```bash
curl -X POST "http://localhost:8000/v1/embed?format=json" \
  -F "file=@/path/to/img.jpg"
```

Binary float32 response example:
```bash
curl -X POST "http://localhost:8000/v1/embed?format=f32" \
  -F "file=@/path/to/img.jpg" \
  --output emb.bin
```
- Response headers contain:
  - `X-Embedding-Dim`
  - `X-Embedding-DType`
  - `X-Model-Version`

Binary payload is **little-endian float32/float16** array of length D.

### 7) (Legacy) Batch embedding
`POST /v1/embed/batch`
- `multipart/form-data` with repeated field `files`
- Query: `format=json|f32|f16`

JSON example:
```bash
curl -X POST "http://localhost:8000/v1/embed/batch?format=json" \
  -F "files=@/path/a.jpg" \
  -F "files=@/path/b.jpg"
```

Binary framed response (recommended for speed):
```bash
curl -X POST "http://localhost:8000/v1/embed/batch?format=f16" \
  -F "files=@/path/a.jpg" \
  -F "files=@/path/b.jpg" \
  --output batch.bin
```

#### Batch binary format: `dogface-batch-v1`
Payload:
- `uint32 N` (little-endian)
- `uint32 D` (little-endian)
- `uint8 dtype_code` (`1=float32`, `2=float16`)
- then `N*D` values (row-major)

Order matches the upload order.

---

## Utilities
### Clear Qdrant collection
```bash
./example_scripts/06_clear_qdrant.sh
```
Use `--hard` to delete the collection itself:
```bash
./example_scripts/06_clear_qdrant.sh --hard
```

---

## Notes / Tuning
- `MAX_CONCURRENCY`: set to 1~2 to avoid GPU OOM.
- `MAX_BATCH_SIZE`: 8~32 is usually reasonable.
- `HF_CACHE_DIR`: mount a persistent volume to avoid re-downloading weights.

### Use finetuned MiewID checkpoint (optional)
Each profile (`verification`, `reid`) is configured independently.
To use a finetuned checkpoint (`backbone+embed+bn`) for Re-ID, set:

```bash
export REID_MODEL_NAME=miewid
export REID_WEIGHT_MODE=ft
export REID_MIEWID_MODEL_SOURCE=models--conservationxlabs--miewid-msv3
export REID_MIEWID_FINETUNE_CKPT_PATH=/workspace/MiewID/src/outputs/train/ft_v2/checkpoints/ep25-val0.5721-miewidv3.ckpt
```

Then restart API. `GET /v1/health` will show `model_version` including ckpt filename when loaded.
