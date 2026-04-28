"""관리자 대시보드용 보조 엔드포인트 모듈."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from app.utils.timezone import business_tz

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db.repositories import ReIdRepository
from app.schemas.admin import AdminImageLabelItem, AdminImageLabelRequest, AdminImageLabelResponse
from app.vector_db.qdrant_store import PointRecord, QdrantStore, build_filter

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database manager not ready")
    return db


def _day_range_ts(day: date) -> Tuple[int, int]:
    tz = business_tz()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return int(start_utc.timestamp()), int(end_utc.timestamp())


def _payload_instance_id(store: QdrantStore, point: PointRecord) -> str:
    pid = point.payload.get("instance_id")
    if isinstance(pid, str) and pid:
        return pid
    return store.external_instance_id(point.point_id)


def _build_label_payload(body: AdminImageLabelRequest, now_ts: int) -> dict:
    action = body.action.upper()
    payload = {
        "label_source": body.source,
        "label_confidence": float(body.confidence),
        "labeled_by": body.labeled_by,
        "labeled_at_ts": now_ts,
        "auto_pet_id": None,
        "auto_score": None,
    }
    if action == "ACCEPT":
        if not body.pet_id:
            raise HTTPException(status_code=400, detail="pet_id is required for action=ACCEPT")
        payload["pet_id"] = body.pet_id
        payload["assignment_status"] = "ACCEPTED"
        return payload
    if action == "REJECT":
        payload["pet_id"] = None
        payload["assignment_status"] = "REJECTED"
        return payload
    payload["pet_id"] = None
    payload["assignment_status"] = "UNREVIEWED"
    return payload


def _sync_meta_sidecars(assignments: Dict[str, dict]) -> None:
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        return

    for meta_path in meta_dir.glob("img_*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        changed = False
        for inst in meta.get("instances") or []:
            instance_id = str(inst.get("instance_id") or "")
            payload = assignments.get(instance_id)
            if payload is None:
                continue
            inst["pet_id"] = payload.get("pet_id")
            inst["auto_pet_id"] = payload.get("auto_pet_id")
            inst["auto_score"] = payload.get("auto_score")
            inst["assignment_status"] = payload.get("assignment_status")
            inst["label_source"] = payload.get("label_source")
            inst["label_confidence"] = payload.get("label_confidence")
            inst["labeled_at_ts"] = payload.get("labeled_at_ts")
            inst["labeled_by"] = payload.get("labeled_by")
            changed = True

        if changed:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _remove_instance_from_meta_sidecars(instance_id: str) -> dict:
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        return {}

    for meta_path in meta_dir.glob("img_*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        instances = list(meta.get("instances") or [])
        kept = [inst for inst in instances if str(inst.get("instance_id") or "") != instance_id]
        if len(kept) == len(instances):
            continue

        image = meta.get("image") or {}
        meta["instances"] = kept
        image["instance_count"] = len(kept)
        meta["image"] = image
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {
            "image_id": str(image.get("image_id") or ""),
            "remaining_instances": len(kept),
        }
    return {}


def _is_seed_point(payload: dict) -> bool:
    if bool(payload.get("is_seed", False)):
        return True
    return str(payload.get("image_role") or "").upper() == "SEED"


def _choose_points(body: AdminImageLabelRequest, points: List[PointRecord]) -> List[PointRecord]:
    if not points:
        return []

    daily_points = [point for point in points if not _is_seed_point(point.payload)]
    if not daily_points:
        return []

    action = body.action.upper()
    if action in ("CLEAR", "REJECT"):
        if body.pet_id:
            matched = [
                point
                for point in daily_points
                if str(point.payload.get("assignment_status") or "").upper() == "ACCEPTED"
                and str(point.payload.get("pet_id") or "").strip() == body.pet_id
            ]
            if matched:
                return matched
        accepted = [
            point
            for point in daily_points
            if str(point.payload.get("assignment_status") or "").upper() == "ACCEPTED"
            and str(point.payload.get("pet_id") or "").strip()
        ]
        if accepted:
            return accepted

    if action == "ACCEPT" and body.pet_id:
        same_pet = [
            point
            for point in daily_points
            if str(point.payload.get("assignment_status") or "").upper() == "ACCEPTED"
            and str(point.payload.get("pet_id") or "").strip() == body.pet_id
        ]
        if same_pet:
            return same_pet

    if body.select_mode == "ALL":
        return daily_points

    return [max(daily_points, key=lambda point: float(point.payload.get("confidence") or 0.0))]


@router.post("/admin/images/labels", response_model=AdminImageLabelResponse)
async def label_images(request: Request, body: AdminImageLabelRequest):
    """Apply admin label actions to image cards rather than raw instance IDs."""
    store = _get_store(request)
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    if body.target_date is not None:
        from_ts, to_ts = _day_range_ts(body.target_date)
    else:
        from_ts, to_ts = None, None
    query_filter = build_filter(
        captured_from_ts=from_ts,
        captured_to_ts=to_ts,
    )
    points = await run_in_threadpool(store.scroll_points, query_filter, 1000, False)

    requested_image_ids = {str(image_id).strip() for image_id in body.image_ids if str(image_id).strip()}
    grouped: Dict[str, List[PointRecord]] = {}
    for point in points:
        image_id = str(point.payload.get("image_id") or "").strip()
        if not image_id or image_id not in requested_image_ids:
            continue
        grouped.setdefault(image_id, []).append(point)

    base_payload = _build_label_payload(body, now_ts)
    meta_sync_payloads: Dict[str, dict] = {}
    items: List[AdminImageLabelItem] = []

    for image_id in body.image_ids:
        current_points = grouped.get(str(image_id), [])
        if not current_points:
            items.append(
                AdminImageLabelItem(
                    image_id=str(image_id),
                    selected_instance_ids=[],
                    updated_count=0,
                    skipped_reason="image_not_found",
                )
            )
            continue

        chosen_points = _choose_points(body, current_points)
        if not chosen_points:
            items.append(
                AdminImageLabelItem(
                    image_id=str(image_id),
                    selected_instance_ids=[],
                    updated_count=0,
                    skipped_reason="no_daily_instances",
                )
            )
            continue

        instance_ids = [_payload_instance_id(store, point) for point in chosen_points]
        await run_in_threadpool(store.set_payload, instance_ids, base_payload)
        for instance_id in instance_ids:
            meta_sync_payloads[instance_id] = dict(base_payload)
        items.append(
            AdminImageLabelItem(
                image_id=str(image_id),
                selected_instance_ids=instance_ids,
                updated_count=len(instance_ids),
            )
        )

    if meta_sync_payloads:
        await run_in_threadpool(_sync_meta_sidecars, meta_sync_payloads)

    return AdminImageLabelResponse(
        action=body.action,
        pet_id=body.pet_id,
        labeled_at=now,
        items=items,
    )


@router.delete("/admin/instances/{instance_id}")
async def delete_instance(request: Request, instance_id: str):
    store = _get_store(request)
    points = await run_in_threadpool(store.retrieve_points, [instance_id], False)
    key = store.external_instance_id(instance_id)
    point = points.get(key)
    if point is None:
        raise HTTPException(status_code=404, detail="instance not found")

    image_id = str(point.payload.get("image_id") or "")
    await run_in_threadpool(store.delete_points, [instance_id])
    sidecar_info = await run_in_threadpool(_remove_instance_from_meta_sidecars, key)
    return {
        "status": "deleted",
        "instance_id": key,
        "image_id": sidecar_info.get("image_id") or image_id,
        "remaining_instances": int(sidecar_info.get("remaining_instances") or 0),
    }


@router.get("/admin/queue/summary")
async def queue_summary(request: Request):
    db = _get_db(request)
    with db.session_scope() as session:
        repo = ReIdRepository(session)
        counts = repo.count_jobs_by_status()
        jobs = repo.list_jobs(limit=500)

    stale_before = datetime.now(timezone.utc) - timedelta(seconds=settings.queue_lease_timeout_s)
    stale_jobs = [
        job
        for job in jobs
        if job.status in {"LEASED", "RUNNING"} and job.heartbeat_at is not None and job.heartbeat_at < stale_before
    ]
    return {
        "queue_enabled": bool(settings.enable_postgres_queue),
        "counts": counts,
        "poll_interval_ms": settings.queue_poll_interval_ms,
        "lease_timeout_s": settings.queue_lease_timeout_s,
        "scheduler_local_capacity": settings.queue_local_capacity,
        "scheduler_max_inflight_jobs": settings.scheduler_max_inflight_jobs,
        "scheduler_micro_batching": bool(settings.scheduler_enable_micro_batching),
        "ingest_batch_pipeline_mode": settings.ingest_batch_pipeline_mode,
        "ingest_pipeline_slots": settings.ingest_pipeline_slots,
        "ingest_pipeline_local_queue_capacity": settings.ingest_pipeline_local_queue_capacity,
        "ingest_job_batch_size": settings.ingest_job_batch_size,
        "ingest_job_batch_max_wait_ms": settings.ingest_job_batch_max_wait_ms,
        "detector_batch_size": settings.detector_batch_size,
        "embedder_crop_batch_size": settings.embedder_crop_batch_size,
        "effective_ingest_images_in_gpu_path": (
            settings.ingest_pipeline_slots
            * (settings.ingest_job_batch_size if settings.ingest_batch_pipeline_mode != "single" else 1)
        ),
        "stale_job_count": len(stale_jobs),
        "stale_job_ids": [str(job.job_id) for job in stale_jobs[:20]],
    }


@router.get("/admin/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
):
    db = _get_db(request)
    with db.session_scope() as session:
        repo = ReIdRepository(session)
        jobs = repo.list_jobs(limit=limit, status=status)
        items = []
        for job in jobs:
            image_id = str(job.payload.get("image_id") or "") or None
            image = repo.get_image(image_id) if image_id else None
            items.append(
                {
                    "job_id": str(job.job_id),
                    "job_type": job.job_type,
                    "status": job.status,
                    "priority": job.priority,
                    "retry_count": job.retry_count,
                    "max_retries": job.max_retries,
                    "locked_by": job.locked_by,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "heartbeat_at": job.heartbeat_at,
                    "image_id": image_id,
                    "image_ingest_status": image.ingest_status if image is not None else None,
                    "image_pipeline_stage": image.pipeline_stage if image is not None else None,
                    "error_code": job.error_code,
                    "error_message": job.error_message,
                }
            )
    return {"items": items, "count": len(items)}


@router.get("/admin/jobs/{job_id}")
async def get_job_detail(request: Request, job_id: str):
    db = _get_db(request)
    try:
        parsed_job_id = uuid.UUID(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid job_id") from e

    with db.session_scope() as session:
        repo = ReIdRepository(session)
        job = repo.get_job(parsed_job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        image_id = str(job.payload.get("image_id") or "") or None
        image = repo.get_image(image_id) if image_id else None
        ingest_request = None
        request_id_raw = job.payload.get("request_id")
        if request_id_raw:
            try:
                ingest_request = repo.get_ingest_request(uuid.UUID(str(request_id_raw)))
            except ValueError:
                ingest_request = None

        return {
            "job": {
                "job_id": str(job.job_id),
                "job_type": job.job_type,
                "status": job.status,
                "priority": job.priority,
                "retry_count": job.retry_count,
                "max_retries": job.max_retries,
                "available_at": job.available_at,
                "locked_by": job.locked_by,
                "locked_at": job.locked_at,
                "heartbeat_at": job.heartbeat_at,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error_code": job.error_code,
                "error_message": job.error_message,
                "payload": job.payload,
                "result": job.result,
            },
            "image": (
                {
                    "image_id": image.image_id,
                    "image_role": image.image_role,
                    "ingest_status": image.ingest_status,
                    "pipeline_stage": image.pipeline_stage,
                    "storage_state": image.storage_state,
                    "last_error_code": image.last_error_code,
                    "last_error_message": image.last_error_message,
                    "raw_path": image.raw_path,
                    "thumb_path": image.thumb_path,
                }
                if image is not None
                else None
            ),
            "ingest_request": (
                {
                    "request_id": str(ingest_request.request_id),
                    "status": ingest_request.status,
                    "request_scope": ingest_request.request_scope,
                    "idempotency_key": ingest_request.idempotency_key,
                    "created_at": ingest_request.created_at,
                    "updated_at": ingest_request.updated_at,
                }
                if ingest_request is not None
                else None
            ),
        }
