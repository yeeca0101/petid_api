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

## Quickstart (Docker Compose)
### 0) Prepare env file
```bash
cp .env.example .env
```

### 1) Put YOLO weights
Copy/mount your YOLO weights to:
`./weights/yolo/yolo26x.pt`

If you use a different filename or segmentation weights, update `.env`:
```bash
YOLO_WEIGHTS_PATH=/app/weights/yolo/yolo26x-seg.pt
YOLO_TASK=segment
```

### 2) Run
```bash
docker compose up --build
```

Optional clean start for development or test runs:
```bash
RESET_DB_ON_START=1 RESET_STORAGE_ON_START=1 docker compose up --build
```
This clears the PostgreSQL schema, deletes the Qdrant collection, and removes generated filesystem storage before the app starts.

Default compose settings:
- API: `http://<server-ip>:8000`
- Qdrant: `http://<server-ip>:6333`
- pgAdmin: `http://<server-ip>:5050`
- `QDRANT_URL` is overridden to `http://qdrant:6333` inside the API container
- `DEVICE` defaults to `cpu`; set `DEVICE=cuda:0` when running on a GPU host

Open:
- Swagger UI: `http://<server-ip>:8000/docs`
- Health: `http://<server-ip>:8000/v1/health`

### Development containers
For local API/frontend editing, use the dev compose file:
```bash
docker compose -f compose.dev.yml up --build
```

Dev endpoints:
- API workspace port: `http://localhost:8009`
- Admin dashboard: `http://localhost:8009/admin`
- Qdrant: `http://localhost:6333`
- pgAdmin: `http://localhost:5050`

Optional clean start:
```bash
RESET_DB_ON_START=1 RESET_STORAGE_ON_START=1 docker compose -f compose.dev.yml up --build
```
This clears the PostgreSQL schema, deletes the Qdrant collection, and removes generated filesystem storage before the app starts.

Dev behavior:
- The API dev container is a workspace container; it does not auto-start FastAPI
- Run the API manually inside the container with `./run_api.sh`
- The whole project is bind-mounted into the API dev container
- Admin static files are served directly by FastAPI at `/admin`
- Dev port can be overridden with `API_DEV_PORT`

---

## Run with provided scripts (no docker-compose)
This repo also includes shell scripts used on the server setup.

1) Start Qdrant
```bash
./run_qdrant.sh
```

2) Start API (loads `.env` and runs on port 8009 by default)
```bash
./run_api.sh
```

3) Start queue worker
```bash
./run_queue_worker.sh
```

4) Build reconciliation report
```bash
./run_reid_reconcile.sh
```



Notes:
- The PostgreSQL-backed queue path requires `ENABLE_POSTGRES_QUEUE=true`
- V1 scheduler policy is single-lane, bounded local queue, strict sequential execution, and no micro-batching
- The queue worker now handles `INGEST_PIPELINE` jobs and performs detect/embed/upsert in the worker process
- Exemplar quick/folder uploads also use the queue path when PostgreSQL queue mode is enabled
- Slice 10 import/reconciliation tooling currently covers filesystem sidecars plus PostgreSQL state; direct Qdrant parity scanning is not included yet
- Slot-based ingest parallelism is now active in the queue worker:
  - `INGEST_PIPELINE_SLOTS` controls ingest pipeline replica count
  - `INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY` controls the shared local dispatch queue for all slots
  - `QUEUE_LOCAL_CAPACITY` still applies inside each slot-local scheduler lane and is not the top-level parallelism knob
- Backpressure in multi-slot mode is enforced by the coordinator so PostgreSQL remains the durable backlog owner.
- `INGEST_PIPELINE_SLOTS=1` preserves the single-slot execution path.

### Local Queue Capacity
`INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY` is the shared in-process dispatch queue size for all slots.

Practical reading:
- `INGEST_PIPELINE_SLOTS` decides how many jobs can execute at the same time.
- `INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY` decides how many claimed jobs can wait in memory before a slot picks them up.
- A larger value absorbs bursts, but also keeps more claimed jobs inside the worker process.
- In most dev/test runs, keeping this close to the slot count or in the low tens is enough.

### Slot-Based Ingest Runtime
Use these environment variables for queue-worker ingest parallelism:

```env
INGEST_PIPELINE_SLOTS=1
INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY=2
```

Operational meaning:
- `INGEST_PIPELINE_SLOTS` is the number of ingest pipeline replicas inside one worker process.
- Each slot owns its own detector/embedder/resource bundle.
- The coordinator claims jobs only when there is free in-process capacity.

### Ingest Batch Pipeline Runtime
The queue worker can batch queued ingest jobs before entering the model pipeline.

Runtime modes:

```env
INGEST_BATCH_PIPELINE_MODE=single
```

- `single`: legacy-safe mode. Process one image job at a time through detector and embedder.
- `batch_embed_only`: collect multiple image jobs, but still run detector one image at a time. Cropped instances are flattened and embedded in crop batches.
- `batch_full`: collect multiple image jobs, run detector with image batches, then flatten cropped instances and run embedder crop batches.

Conservative local starting point:

```env
ENABLE_POSTGRES_QUEUE=true
INGEST_PIPELINE_SLOTS=1
INGEST_BATCH_PIPELINE_MODE=batch_full
INGEST_JOB_BATCH_SIZE=8
INGEST_JOB_BATCH_MAX_WAIT_MS=100
DETECTOR_BATCH_SIZE=8
EMBEDDER_CROP_BATCH_SIZE=32
```

Backpressure rule of thumb:

```text
effective images in GPU path = INGEST_PIPELINE_SLOTS * INGEST_JOB_BATCH_SIZE
```

For example, `INGEST_PIPELINE_SLOTS=1` and `INGEST_JOB_BATCH_SIZE=8` means at most one 8-image model batch is in the GPU path. If each image produces 8 crops on average, the embedder may see 64 crops, which are chunked by `EMBEDDER_CROP_BATCH_SIZE`.

Rollback:

```env
INGEST_BATCH_PIPELINE_MODE=single
```

Use `/v1/admin/queue/summary` to verify the active mode and configured batch sizes.

### Dev Validation Note
Confirmed on 2026-04-22:
- `docker compose -f compose.dev.yml up -d` was used with 2-slot settings and the dev stack started successfully.

Example 2-slot dev worker settings:
```env
INGEST_PIPELINE_SLOTS=2
INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY=2
```

### Slot Recommendation Tool
Use the built-in recommendation CLI with a probe image to measure a conservative slot count for the current machine:

```bash
python3 -m app.tools.ingest_slot_recommend --probe-runtime
```

To see the full CLI help and flags:

```bash
python3 -m app.tools.ingest_slot_recommend --help
```

Runtime probe behavior:
- Requires `INGEST_PIPELINE_PROBE_IMAGE` to point to a readable image file.
- Builds the detector/embedder stack used by the ingest worker.
- Runs detect -> crop -> embed without DB writes or Qdrant upserts.
- Reports measured RAM deltas and, when CUDA stats are available, measured VRAM deltas.
- Reuses `INGEST_PIPELINE_RECOMMEND_SAFETY_VRAM_GB` and `INGEST_PIPELINE_RECOMMEND_SAFETY_RAM_GB`.
- Run this mode inside the dev container or another Python environment where the app dependencies and model weights are available.


Example with the dev container:

```bash
docker exec -it petid_api_dev python3 -m app.tools.ingest_slot_recommend --probe-runtime
```

### Rollout Validation Checklist
Use this checklist before raising slot count in a new environment:

1. Set `INGEST_PIPELINE_PROBE_IMAGE` to a representative daily image.
2. Run `python3 -m app.tools.ingest_slot_recommend --probe-runtime` in the worker environment.
3. Start with the recommended slot count or lower.
4. Confirm that `INGEST_PIPELINE_SLOTS=2` initializes two slot worker ids in logs when testing 2-slot mode.
5. Confirm that jobs move through `LEASED` and `RUNNING` with slot-specific worker ids.
6. Confirm that one running slot does not block the other slot from starting the next job.
7. Watch queue/admin endpoints during a small burst and confirm PostgreSQL remains the durable backlog owner.

### Example Starting Points
These are conservative starting points for rollout planning, not guaranteed limits:

24GB GPU host:
```env
INGEST_PIPELINE_SLOTS=2
INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY=2
```

48GB GPU host:
```env
INGEST_PIPELINE_SLOTS=4
INGEST_PIPELINE_LOCAL_QUEUE_CAPACITY=4
```

Notes:
- Final slot count should still be adjusted using `--probe-runtime` in the target environment.
- Host RAM can become the limiting resource before VRAM does.
- Increase slot count gradually and verify queue/admin health after each change.

Open:
- Swagger UI: `http://<server-ip>:8009/docs`
- Health: `http://<server-ip>:8009/v1/health`
- Queue health: `http://<server-ip>:8009/v1/health/queue`
- Admin queue summary: `http://<server-ip>:8009/v1/admin/queue/summary`
- Admin jobs list: `http://<server-ip>:8009/v1/admin/jobs`

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

### Reconciliation report
```bash
./run_reid_reconcile.sh
```

Write JSON and fail when cutover blockers remain:
```bash
./run_reid_reconcile.sh --format json --output ./data/shared/reid_reconcile.json --fail-on-cutover-blockers
```

Current import scope:
- imports `images` and `instances` from legacy sidecar metadata
- reports registry entry counts but does not import pets because no `pets` table exists yet
- skips assignment history/current-state import because `instance_assignments` is not part of the current V1 schema
- uses DB/state-based reconciliation for vector parity; it does not scan Qdrant contents directly

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
