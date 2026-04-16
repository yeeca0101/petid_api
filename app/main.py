from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.lifecycle import dispose_db_state, init_db_state
from app.ml.embedder import Embedder
from app.vector_db.qdrant_store import QdrantStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    init_db_state(app)
    verification_embedder = Embedder(
        settings,
        profile_name="verification",
        model_name=settings.verification_model_name,
        miewid_model_source=settings.verification_miewid_model_source,
        miewid_finetune_ckpt_path=settings.verification_miewid_finetune_ckpt_path,
        weight_mode=settings.verification_weight_mode,
    )
    reid_embedder = Embedder(
        settings,
        profile_name="reid",
        model_name=settings.reid_model_name,
        miewid_model_source=settings.reid_miewid_model_source,
        miewid_finetune_ckpt_path=settings.reid_miewid_finetune_ckpt_path,
        weight_mode=settings.reid_weight_mode,
    )
    app.state.embedders = {
        "verification": verification_embedder,
        "reid": reid_embedder,
    }
    # Backward compatibility for endpoints that still expect a single embedder.
    app.state.embedder = verification_embedder

    # Vector DB
    store = QdrantStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        collection=settings.qdrant_collection,
        timeout_s=settings.qdrant_timeout_s,
    )
    # Ensure collection exists with correct vector size.
    if reid_embedder.dim is None:
        raise RuntimeError("Failed to resolve embedding dimension")
    logger.info(
        "Connecting to Qdrant | url=%s | collection=%s | timeout_s=%s",
        settings.qdrant_url,
        settings.qdrant_collection,
        settings.qdrant_timeout_s,
    )
    try:
        store.ensure_collection(reid_embedder.dim)
    except Exception:
        logger.exception(
            "Qdrant connection failed | url=%s | collection=%s | "
            "hint: if running locally, start ./run_qdrant.sh and use QDRANT_URL=http://localhost:6333; "
            "if running with docker-compose, use the container service URL such as http://qdrant:6333",
            settings.qdrant_url,
            settings.qdrant_collection,
        )
        raise
    app.state.vector_store = store

    # Detector (optional)
    if settings.detector_enabled:
        from app.ml.detector import YoloDetector

        keep_ids = [int(x.strip()) for x in settings.yolo_class_ids.split(",") if x.strip()]
        det = YoloDetector(
            weights_path=settings.yolo_weights_path,
            device=settings.device,
            imgsz=settings.yolo_imgsz,
            conf=settings.yolo_conf,
            iou=settings.yolo_iou,
            keep_class_ids=keep_ids,
            task=settings.yolo_task,
        )
        app.state.detector = det
    yield
    dispose_db_state(app)
    # No explicit teardown for PoC ML/vector resources


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(v1_router, prefix=settings.api_prefix)
admin_dir = Path(__file__).resolve().parents[1] / "for_admin"
if admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_prefix}/health",
    }
