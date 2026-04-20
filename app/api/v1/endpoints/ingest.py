"""이미지 업로드, 탐지, 임베딩, 저장 파이프라인을 처리하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from app.utils.timezone import business_tz

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db.repositories import ReIdRepository
from app.ml.cropper import NormalizedBBox, crop_from_bbox, pad_bbox
from app.schemas.ingest import (
    BBox,
    EmbeddingMeta,
    IngestAcceptedImage,
    IngestAcceptedResponse,
    IngestStatusResponse,
    ImageMeta,
    IngestResponse,
    InstanceOut,
)
from app.utils.image_io import load_pil_image
from app.vector_db.qdrant_store import QdrantStore

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_species(class_id: int) -> str:
    # Based on your plan: {Cat:15, Dog:16}
    if class_id == 15:
        return "CAT"
    if class_id == 16:
        return "DOG"
    return "UNKNOWN"


def _safe_folder_name(name: Optional[str]) -> str:
    if not name:
        return "unknown"
    safe = name.strip().replace("/", "_")
    if os.altsep:
        safe = safe.replace(os.altsep, "_")
    return safe or "unknown"


def _bbox_area_ratio(det) -> float:
    return max(0.0, float(det.x2) - float(det.x1)) * max(0.0, float(det.y2) - float(det.y1))


def _seed_detection_sort_key(det) -> tuple[float, float, float]:
    cx = (float(det.x1) + float(det.x2)) * 0.5
    cy = (float(det.y1) + float(det.y2)) * 0.5
    center_dist_sq = (cx - 0.5) ** 2 + (cy - 0.5) ** 2
    area = _bbox_area_ratio(det)
    conf = float(getattr(det, "confidence", 0.0))
    return (center_dist_sq, -area, -conf)


def _should_apply_min_bbox_area(image_role: str) -> bool:
    if image_role == "SEED":
        return bool(settings.apply_min_bbox_area_to_seed)
    return bool(settings.apply_min_bbox_area_to_daily)


def _filter_small_detections(detections, image_role: str):
    if not _should_apply_min_bbox_area(image_role):
        return detections
    min_area = float(settings.min_bbox_area_ratio)
    if min_area <= 0.0:
        return detections
    return [d for d in detections if _bbox_area_ratio(d) >= min_area]


def _detection_to_source_meta(det) -> dict:
    bb = NormalizedBBox(x1=float(det.x1), y1=float(det.y1), x2=float(det.x2), y2=float(det.y2))
    bb = pad_bbox(bb, settings.crop_padding)
    return {
        "class_id": int(det.class_id),
        "species": _parse_species(int(det.class_id)),
        "confidence": float(det.confidence),
        "bbox": {"x1": bb.x1, "y1": bb.y1, "x2": bb.x2, "y2": bb.y2},
    }


def _business_date_folder(dt: datetime) -> str:
    return dt.astimezone(business_tz()).date().isoformat()


def _parse_captured_at(captured_at: Optional[str]) -> Optional[datetime]:
    if not captured_at:
        return None
    try:
        parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=business_tz())
        return parsed
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid captured_at: {captured_at}") from e


async def _persist_uploaded_image(
    *,
    file: UploadFile,
    img,
    image_id: str,
    image_role: str,
    pet_name: Optional[str],
    captured_dt: Optional[datetime],
    uploaded_at: datetime,
) -> tuple[Path, Path]:
    base_dir = Path(settings.reid_storage_dir)
    role_dir = image_role.lower()
    if image_role == "SEED":
        pet_dir = _safe_folder_name(pet_name)
        raw_dir = base_dir / "images" / role_dir / pet_dir
        thumb_dir = base_dir / "thumbs" / role_dir / pet_dir
    else:
        daily_folder = _business_date_folder(captured_dt or uploaded_at)
        raw_dir = base_dir / "images" / role_dir / daily_folder
        thumb_dir = base_dir / "thumbs" / role_dir / daily_folder

    raw_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    raw_path = raw_dir / f"{image_id}{ext}"
    await run_in_threadpool(img.save, raw_path)

    thumb_path = thumb_dir / f"{image_id}.jpg"

    def _make_thumb():
        t = img.copy()
        if t.mode not in ("RGB", "L"):
            t = t.convert("RGB")
        t.thumbnail((settings.thumbnail_max_side_px, settings.thumbnail_max_side_px))
        t.save(thumb_path, format="JPEG", quality=85)

    await run_in_threadpool(_make_thumb)
    return raw_path, thumb_path


def _get_embedder(request: Request):
    embedders = getattr(request.app.state, "embedders", None)
    if isinstance(embedders, dict):
        embedder = embedders.get("reid")
        if embedder is not None:
            return embedder
    embedder = getattr(request.app.state, "embedder", None)
    if embedder is None:
        raise HTTPException(status_code=503, detail="Embedding model not ready")
    return embedder


def _get_detector(request: Request):
    det = getattr(request.app.state, "detector", None)
    if det is None and settings.detector_enabled:
        raise HTTPException(status_code=503, detail="Detector not ready")
    return det


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


@router.post("/ingest", response_model=IngestResponse | IngestAcceptedResponse)
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    daycare_id: Optional[str] = Form(default=None),
    trainer_id: Optional[str] = Form(default=None),
    captured_at: Optional[str] = Form(default=None, description="ISO8601 timestamp"),
    image_role: Literal["DAILY", "SEED"] = Form(default="DAILY"),
    pet_name: Optional[str] = Form(default=None, description="Used for SEED storage subdirectory"),
    include_embedding: bool = Query(default=False, description="Include vectors in response (debug)."),
    x_idempotency_key: Optional[str] = Header(default=None),
):
    """Upload an image, detect pets, embed each detected instance, and store in vector DB."""

    img = await load_pil_image(file, settings.max_image_bytes)
    w, h = img.size

    cap_dt = _parse_captured_at(captured_at)
    resolved_daycare_id = (daycare_id or "").strip()
    uploaded_at = _utcnow()
    image_id = f"img_{uuid.uuid4().hex}"
    raw_path, thumb_path = await _persist_uploaded_image(
        file=file,
        img=img,
        image_id=image_id,
        image_role=image_role,
        pet_name=pet_name,
        captured_dt=cap_dt,
        uploaded_at=uploaded_at,
    )

    if settings.enable_postgres_queue:
        db = getattr(request.app.state, "db", None)
        if db is None:
            raise HTTPException(status_code=503, detail="Database manager not ready")

        request_scope = "ingest"
        idempotency_key = (x_idempotency_key or "").strip() or f"ingest:{image_id}"
        request_hash = f"{file.filename or 'upload'}:{w}:{h}:{int(uploaded_at.timestamp())}"
        business_date = (cap_dt or uploaded_at).astimezone(business_tz()).date()

        with db.session_scope() as session:
            repo = ReIdRepository(session)
            existing = repo.get_ingest_request_by_scope_key(
                request_scope=request_scope,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                existing_job = repo.get_job_by_request_id(existing.request_id)
                existing_image = repo.get_image(existing.image_id) if existing.image_id else None
                if existing_job is not None and existing_image is not None:
                    return IngestAcceptedResponse(
                        request_id=str(existing.request_id),
                        job_id=str(existing_job.job_id),
                        status_url=f"{settings.api_prefix}/ingest/requests/{existing.request_id}",
                        image=IngestAcceptedImage(
                            image_id=existing_image.image_id,
                            image_role=existing_image.image_role,
                            uploaded_at=existing_image.uploaded_at,
                            width=existing_image.width,
                            height=existing_image.height,
                            storage_path=existing_image.raw_path,
                            thumb_path=existing_image.thumb_path,
                            ingest_status=existing_image.ingest_status,
                            pipeline_stage=existing_image.pipeline_stage,
                        ),
                    )

            image = repo.create_image(
                image_id=image_id,
                image_role=image_role,
                daycare_id=resolved_daycare_id or None,
                trainer_id=(trainer_id or "").strip() or None,
                input_pet_name=(pet_name or "").strip() or None,
                captured_at=cap_dt,
                uploaded_at=uploaded_at,
                business_date=business_date,
                original_filename=file.filename or None,
                mime_type=file.content_type or None,
                file_size_bytes=None,
                width=w,
                height=h,
                raw_path=str(raw_path),
                thumb_path=str(thumb_path),
                storage_state="READY",
                ingest_status="PENDING",
                pipeline_stage="STORED",
                pipeline_version="yolo26x+miewidv3+poc",
            )
            ingest_request = repo.create_ingest_request(
                idempotency_key=idempotency_key,
                request_scope=request_scope,
                status="RECEIVED",
                image_id=image.image_id,
                request_hash=request_hash,
            )
            job = repo.enqueue_job(
                job_type="INGEST_PIPELINE",
                dedupe_key=image.image_id,
                payload={
                    "request_id": str(ingest_request.request_id),
                    "image_id": image.image_id,
                    "image_role": image.image_role,
                    "trainer_id": image.trainer_id,
                    "daycare_id": image.daycare_id,
                    "include_embedding": include_embedding,
                },
                status="QUEUED",
                priority=100,
                max_retries=settings.queue_max_retries_default,
            )
            repo.append_job_event(
                job_id=job.job_id,
                event_type="JOB_ENQUEUED",
                payload={"request_id": str(ingest_request.request_id), "image_id": image.image_id},
            )

        return IngestAcceptedResponse(
            request_id=str(ingest_request.request_id),
            job_id=str(job.job_id),
            status_url=f"{settings.api_prefix}/ingest/requests/{ingest_request.request_id}",
            image=IngestAcceptedImage(
                image_id=image.image_id,
                image_role=image.image_role,
                uploaded_at=image.uploaded_at,
                width=image.width,
                height=image.height,
                storage_path=image.raw_path,
                thumb_path=image.thumb_path,
                ingest_status=image.ingest_status,
                pipeline_stage=image.pipeline_stage,
            ),
        )

    embedder = _get_embedder(request)
    detector = _get_detector(request)
    store = _get_store(request)

    base_dir = Path(settings.reid_storage_dir)
    meta_dir = base_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    # Detect instances
    detections = []
    if settings.detector_enabled:
        detections = await run_in_threadpool(detector.detect, img)
    else:
        # Fallback: treat whole image as a single instance
        detections = [
            type("_D", (), {"class_id": 16, "confidence": 1.0, "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0})
        ]

    # Optional minimum area filter for tiny detections.
    detections = _filter_small_detections(detections, image_role=image_role)
    source_detections = [_detection_to_source_meta(d) for d in detections]
    primary_source_detection_index: Optional[int] = None

    # Seed policy: one exemplar instance per image.
    # Prefer the detection nearest the image center, then the largest bbox, then confidence.
    if image_role == "SEED" and len(detections) > 1:
        primary_source_detection_index = min(
            range(len(detections)), key=lambda idx: _seed_detection_sort_key(detections[idx])
        )
        detections = [detections[primary_source_detection_index]]
    elif image_role == "SEED" and len(detections) == 1:
        primary_source_detection_index = 0

    # Crop instances
    crops = []
    inst_meta = []
    for d in detections:
        bb = NormalizedBBox(x1=float(d.x1), y1=float(d.y1), x2=float(d.x2), y2=float(d.y2))
        bb = pad_bbox(bb, settings.crop_padding)
        crop = crop_from_bbox(img, bb)
        crops.append(crop)
        inst_meta.append((d, bb))

    # Embed in batch
    embs = []
    if crops:
        async with embedder.semaphore:
            embs = await run_in_threadpool(embedder.embed_pil_images, crops)

    # Upsert into vector DB as instance-level points
    from qdrant_client.http import models as qm

    points = []
    instances_out = []
    meta_instances = []
    for i, (d, bb) in enumerate(inst_meta):
        instance_uuid = str(uuid.uuid4())
        instance_id = f"ins_{instance_uuid}"
        emb_vec = embs[i].tolist() if len(embs) else []
        species = _parse_species(int(d.class_id))
        cap_ts = int((cap_dt or uploaded_at).timestamp())

        payload = {
            "daycare_id": resolved_daycare_id,
            "trainer_id": trainer_id,
            "image_id": image_id,
            "image_role": image_role,
            "pet_name": pet_name,
            "captured_at_ts": cap_ts,
            "species": species,
            "class_id": int(d.class_id),
            "det_conf": float(d.confidence),
            "bbox": {"x1": bb.x1, "y1": bb.y1, "x2": bb.x2, "y2": bb.y2},
            "embedding_type": "BODY",
            "model_version": embedder.model_info.model_version,
            "instance_id": instance_id,
        }

        points.append(qm.PointStruct(id=instance_uuid, vector=emb_vec, payload=payload))

        meta_instances.append(
            {
                "instance_id": instance_id,
                "class_id": int(d.class_id),
                "species": species,
                "confidence": float(d.confidence),
                "bbox": {"x1": bb.x1, "y1": bb.y1, "x2": bb.x2, "y2": bb.y2},
                "pet_id": None,
            }
        )

        inst_out = InstanceOut(
            instance_id=instance_id,
            class_id=int(d.class_id),
            species=species,
            confidence=float(d.confidence),
            bbox=BBox(x1=bb.x1, y1=bb.y1, x2=bb.x2, y2=bb.y2),
            embedding=emb_vec if include_embedding else None,
            embedding_meta=(
                EmbeddingMeta(
                    embedding_type="BODY",
                    dim=int(len(emb_vec)),
                    dtype="float32",
                    l2_normalized=True,
                    model_version=embedder.model_info.model_version,
                )
                if emb_vec
                else None
            ),
        )
        instances_out.append(inst_out)

    # Write points
    await run_in_threadpool(store.upsert, points)

    # Write metadata sidecar (used for server gallery)
    cap_ts = int((cap_dt or uploaded_at).timestamp())
    meta = {
        "image": {
            "image_id": image_id,
            "daycare_id": resolved_daycare_id,
            "image_role": image_role,
            "pet_name": pet_name,
            "trainer_id": trainer_id,
            "captured_at": (cap_dt.isoformat() if cap_dt else None),
            "uploaded_at": uploaded_at.isoformat(),
            "original_filename": (file.filename or None),
            "captured_at_ts": cap_ts,
            "uploaded_at_ts": int(uploaded_at.timestamp()),
            "width": w,
            "height": h,
            "raw_path": str(raw_path),
            "thumb_path": str(thumb_path),
            "raw_url": f"{settings.api_prefix}/images/{image_id}?variant=raw",
            "thumb_url": f"{settings.api_prefix}/images/{image_id}?variant=thumb",
            "instance_count": len(meta_instances),
            "pipeline_version": "yolo26x+miewidv3+poc",
        },
        "instances": meta_instances,
        "source_detections": source_detections if image_role == "SEED" else None,
        "primary_source_detection_index": primary_source_detection_index if image_role == "SEED" else None,
    }
    (meta_dir / f"{image_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    return IngestResponse(
        image=ImageMeta(
            image_id=image_id,
            image_role=image_role,
            captured_at=cap_dt,
            uploaded_at=uploaded_at,
            width=w,
            height=h,
            storage_path=str(raw_path),
        ),
        instances=instances_out,
    )


@router.get("/ingest/requests/{request_id}", response_model=IngestStatusResponse)
async def get_ingest_request_status(request: Request, request_id: str):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database manager not ready")

    try:
        parsed_request_id = uuid.UUID(request_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid request_id") from e

    with db.session_scope() as session:
        repo = ReIdRepository(session)
        ingest_request = repo.get_ingest_request(parsed_request_id)
        if ingest_request is None:
            raise HTTPException(status_code=404, detail="Ingest request not found")
        image = repo.get_image(ingest_request.image_id) if ingest_request.image_id else None
        job = repo.get_job_by_request_id(parsed_request_id)

        return IngestStatusResponse(
            request_id=str(ingest_request.request_id),
            request_status=ingest_request.status,
            image_id=image.image_id if image is not None else ingest_request.image_id,
            job_id=(str(job.job_id) if job is not None else None),
            job_status=(job.status if job is not None else None),
            image_role=(image.image_role if image is not None else None),
            ingest_status=(image.ingest_status if image is not None else None),
            pipeline_stage=(image.pipeline_stage if image is not None else None),
            created_at=ingest_request.created_at,
            updated_at=ingest_request.updated_at,
        )
