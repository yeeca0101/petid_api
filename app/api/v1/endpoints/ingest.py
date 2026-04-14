"""이미지 업로드, 탐지, 임베딩, 저장 파이프라인을 처리하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from app.utils.timezone import business_tz

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.ml.cropper import NormalizedBBox, crop_from_bbox, pad_bbox
from app.schemas.ingest import (
    BBox,
    EmbeddingMeta,
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


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    daycare_id: Optional[str] = Form(default=None),
    trainer_id: Optional[str] = Form(default=None),
    captured_at: Optional[str] = Form(default=None, description="ISO8601 timestamp"),
    image_role: Literal["DAILY", "SEED"] = Form(default="DAILY"),
    pet_name: Optional[str] = Form(default=None, description="Used for SEED storage subdirectory"),
    include_embedding: bool = Query(default=False, description="Include vectors in response (debug)."),
):
    """Upload an image, detect pets, embed each detected instance, and store in vector DB."""

    embedder = _get_embedder(request)
    detector = _get_detector(request)
    store = _get_store(request)

    img = await load_pil_image(file, settings.max_image_bytes)
    w, h = img.size

    # Parse captured_at if provided
    cap_dt: Optional[datetime] = None
    if captured_at:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # If client omits timezone, treat it as business timezone (default KST).
                parsed = parsed.replace(tzinfo=business_tz())
            cap_dt = parsed
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid captured_at: {captured_at}") from e

    resolved_daycare_id = (daycare_id or "").strip()
    uploaded_at = _utcnow()
    image_id = f"img_{uuid.uuid4().hex}"

    # Persist raw image (PoC local storage)
    base_dir = Path(settings.reid_storage_dir)
    role_dir = image_role.lower()
    if image_role == "SEED":
        pet_dir = _safe_folder_name(pet_name)
        raw_dir = base_dir / "images" / role_dir / pet_dir
        thumb_dir = base_dir / "thumbs" / role_dir / pet_dir
    else:
        daily_folder = _business_date_folder(cap_dt or uploaded_at)
        raw_dir = base_dir / "images" / role_dir / daily_folder
        thumb_dir = base_dir / "thumbs" / role_dir / daily_folder
    meta_dir = base_dir / "meta"
    raw_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    raw_path = raw_dir / f"{image_id}{ext}"
    await run_in_threadpool(img.save, raw_path)

    # Create thumbnail
    thumb_path = thumb_dir / f"{image_id}.jpg"

    def _make_thumb():
        t = img.copy()
        if t.mode not in ("RGB", "L"):
            t = t.convert("RGB")
        t.thumbnail((settings.thumbnail_max_side_px, settings.thumbnail_max_side_px))
        t.save(thumb_path, format="JPEG", quality=85)

    await run_in_threadpool(_make_thumb)

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
