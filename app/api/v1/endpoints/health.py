"""서비스 및 임베딩 모델 상태를 점검하는 헬스체크 엔드포인트 모듈."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.core.config import settings
from app.db.repositories import ReIdRepository

router = APIRouter()


@router.get("/health")
def health(request: Request):
    model = None
    models = {}

    embedders = getattr(request.app.state, "embedders", None)
    if isinstance(embedders, dict):
        for profile, embedder in embedders.items():
            models[str(profile)] = {
                "model_name": embedder.model_info.model_name,
                "model_version": embedder.model_info.model_version,
                "input_size": embedder.model_info.input_size,
                "dim": embedder.dim,
                "device": str(embedder.device),
            }
        model = models.get("verification")
    else:
        embedder = getattr(request.app.state, "embedder", None)
        if embedder is not None:
            model = {
                "model_name": embedder.model_info.model_name,
                "model_version": embedder.model_info.model_version,
                "input_size": embedder.model_info.input_size,
                "dim": embedder.dim,
                "device": str(embedder.device),
            }

    return {
        "status": "ok",
        "model": model,
        "models": models if models else None,
        "postgres_queue_enabled": bool(settings.enable_postgres_queue),
    }


@router.get("/health/qdrant")
def qdrant_health(request: Request):
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        return {"status": "not_ready", "qdrant": None}

    try:
        collection = store.client.get_collection(store.collection)
        points_count = store.client.count(collection_name=store.collection, exact=True).count

        # Some Qdrant builds return null for vectors_count/indexed_vectors_count.
        # To make status practical, sample a small page with vectors included.
        sample_points, _ = store.client.scroll(
            collection_name=store.collection,
            limit=64,
            with_payload=False,
            with_vectors=True,
        )
        sampled_points = len(sample_points)
        sampled_with_vector = 0
        sampled_vector_dim = None
        meta_dir = Path(settings.reid_storage_dir) / "meta"
        total_images = len(list(meta_dir.glob("*.json"))) if meta_dir.exists() else 0
        for p in sample_points:
            vec = getattr(p, "vector", None)
            if vec is None:
                continue
            sampled_with_vector += 1
            if sampled_vector_dim is None:
                try:
                    sampled_vector_dim = len(vec)  # dense single-vector collection
                except Exception:
                    sampled_vector_dim = None

        return {
            "status": "ok",
            "qdrant": {
                "collection": store.collection,
                "vectors_count": getattr(collection, "vectors_count", None),
                "points_count": int(points_count),
                "total_images": int(total_images),
                "indexed_vectors_count": getattr(collection, "indexed_vectors_count", None),
                "status": str(getattr(collection, "status", None)),
                "sampled_points": sampled_points,
                "sampled_with_vector": sampled_with_vector,
                "sampled_has_vector": sampled_with_vector > 0,
                "sampled_vector_dim": sampled_vector_dim,
            },
        }
    except Exception as e:
        return {"status": "error", "qdrant": {"collection": store.collection}, "error": str(e)}


@router.get("/health/queue")
def queue_health(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"status": "not_ready", "queue": None}

    try:
        with db.session_scope() as session:
            repo = ReIdRepository(session)
            counts = repo.count_jobs_by_status()
            jobs = repo.list_jobs(limit=200)

        stale_before = datetime.now(timezone.utc) - timedelta(seconds=settings.queue_lease_timeout_s)
        stale_jobs = [
            job
            for job in jobs
            if job.status in {"LEASED", "RUNNING"} and job.heartbeat_at is not None and job.heartbeat_at < stale_before
        ]
        return {
            "status": "ok",
            "queue": {
                "enabled": bool(settings.enable_postgres_queue),
                "counts": counts,
                "stale_job_count": len(stale_jobs),
                "scheduler_local_capacity": settings.queue_local_capacity,
                "scheduler_max_inflight_jobs": settings.scheduler_max_inflight_jobs,
            },
        }
    except Exception as e:
        return {"status": "error", "queue": None, "error": str(e)}
