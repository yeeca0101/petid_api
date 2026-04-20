from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps
from qdrant_client.http import models as qm

from app.core.config import Settings
from app.db.repositories import ReIdRepository
from app.db.session import DatabaseManager
from app.ml.cropper import NormalizedBBox, crop_from_bbox, pad_bbox
from app.ml.detector import YoloDetector
from app.ml.embedder import Embedder
from app.utils.timezone import business_tz
from app.vector_db.qdrant_store import QdrantStore
from app.worker.scheduler import SchedulerTask, SingleLaneScheduler

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_species(class_id: int) -> str:
    if class_id == 15:
        return "CAT"
    if class_id == 16:
        return "DOG"
    return "UNKNOWN"


def _bbox_area_ratio(det) -> float:
    return max(0.0, float(det.x2) - float(det.x1)) * max(0.0, float(det.y2) - float(det.y1))


def _seed_detection_sort_key(det) -> tuple[float, float, float]:
    cx = (float(det.x1) + float(det.x2)) * 0.5
    cy = (float(det.y1) + float(det.y2)) * 0.5
    center_dist_sq = (cx - 0.5) ** 2 + (cy - 0.5) ** 2
    area = _bbox_area_ratio(det)
    conf = float(getattr(det, "confidence", 0.0))
    return (center_dist_sq, -area, -conf)


def _should_apply_min_bbox_area(settings: Settings, image_role: str) -> bool:
    if image_role == "SEED":
        return bool(settings.apply_min_bbox_area_to_seed)
    return bool(settings.apply_min_bbox_area_to_daily)


def _filter_small_detections(settings: Settings, detections, image_role: str):
    if not _should_apply_min_bbox_area(settings, image_role):
        return detections
    min_area = float(settings.min_bbox_area_ratio)
    if min_area <= 0.0:
        return detections
    return [d for d in detections if _bbox_area_ratio(d) >= min_area]


def _detection_to_source_meta(settings: Settings, det) -> dict[str, Any]:
    bb = NormalizedBBox(x1=float(det.x1), y1=float(det.y1), x2=float(det.x2), y2=float(det.y2))
    bb = pad_bbox(bb, settings.crop_padding)
    return {
        "class_id": int(det.class_id),
        "species": _parse_species(int(det.class_id)),
        "confidence": float(det.confidence),
        "bbox": {"x1": bb.x1, "y1": bb.y1, "x2": bb.x2, "y2": bb.y2},
    }


def _load_pil_image_from_path(path: str) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


@dataclass(frozen=True)
class WorkerResources:
    settings: Settings
    embedder: Embedder
    detector: Optional[YoloDetector]
    store: QdrantStore


def build_worker_resources(settings: Settings) -> WorkerResources:
    embedder = Embedder(
        settings,
        profile_name="reid-worker",
        model_name=settings.reid_model_name,
        miewid_model_source=settings.reid_miewid_model_source,
        miewid_finetune_ckpt_path=settings.reid_miewid_finetune_ckpt_path,
        weight_mode=settings.reid_weight_mode,
    )
    if embedder.dim is None:
        raise RuntimeError("Failed to resolve worker embedding dimension")

    store = QdrantStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        collection=settings.qdrant_collection,
        timeout_s=settings.qdrant_timeout_s,
    )
    store.ensure_collection(embedder.dim)

    detector: Optional[YoloDetector] = None
    if settings.detector_enabled:
        keep_ids = [int(x.strip()) for x in settings.yolo_class_ids.split(",") if x.strip()]
        detector = YoloDetector(
            weights_path=settings.yolo_weights_path,
            device=settings.device,
            imgsz=settings.yolo_imgsz,
            conf=settings.yolo_conf,
            iou=settings.yolo_iou,
            keep_class_ids=keep_ids,
            task=settings.yolo_task,
        )

    return WorkerResources(settings=settings, embedder=embedder, detector=detector, store=store)


def execute_ingest_pipeline(
    *,
    db: DatabaseManager,
    scheduler: SingleLaneScheduler,
    resources: WorkerResources,
    job_id: uuid.UUID,
    payload: dict[str, Any],
) -> dict[str, Any]:
    image_id = str(payload["image_id"])
    request_id_raw = payload.get("request_id")
    request_id = uuid.UUID(request_id_raw) if request_id_raw else None

    try:
        with db.session_scope() as session:
            repo = ReIdRepository(session)
            image = repo.get_image(image_id)
            if image is None:
                raise LookupError(f"image not found for ingest pipeline: {image_id}")
            repo.update_image_status(image_id, ingest_status="PROCESSING", pipeline_stage="WAITING_FOR_SCHEDULER")
            if request_id is not None:
                repo.update_ingest_request_status(request_id, status="PROCESSING", image_id=image_id)
            repo.append_job_event(job_id=job_id, event_type="IMAGE_WAITING_FOR_SCHEDULER", payload={"image_id": image_id})
            raw_path = image.raw_path

        pil_image = _load_pil_image_from_path(raw_path)
        processed = scheduler.submit(
            SchedulerTask(
                job_id=job_id,
                job_type="INGEST_PIPELINE",
                payload=payload,
                fn=lambda: _run_gpu_ingest_steps(
                    db=db,
                    resources=resources,
                    job_id=job_id,
                    image_id=image_id,
                    pil_image=pil_image,
                    payload=payload,
                ),
            )
        )

        with db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.update_image_status(
                image_id,
                pipeline_stage="UPSERTING_VECTOR",
                source_detection_count=processed["source_detection_count"],
                primary_source_detection_index=processed["primary_source_detection_index"],
            )
            repo.append_job_event(
                job_id=job_id,
                event_type="IMAGE_UPSERTING_VECTOR",
                payload={"image_id": image_id, "instance_count": len(processed["instances"])},
            )

        resources.store.upsert(processed["points"])

        instance_rows = [
            {
                "instance_id": item["instance_id"],
                "species": item["species"],
                "class_id": item["class_id"],
                "det_conf": item["confidence"],
                "bbox_x1": item["bbox"]["x1"],
                "bbox_y1": item["bbox"]["y1"],
                "bbox_x2": item["bbox"]["x2"],
                "bbox_y2": item["bbox"]["y2"],
                "qdrant_point_id": item["instance_id"],
                "vector_status": "READY",
                "embedding_type": "BODY",
                "model_version": processed["model_version"],
            }
            for item in processed["instances"]
        ]

        with db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.replace_instances_for_image(image_id, instances=instance_rows)
            repo.update_image_status(
                image_id,
                ingest_status="READY",
                pipeline_stage="READY",
                source_detection_count=processed["source_detection_count"],
                primary_source_detection_index=processed["primary_source_detection_index"],
            )
            if request_id is not None:
                repo.update_ingest_request_status(request_id, status="SUCCEEDED", image_id=image_id)
            repo.append_job_event(
                job_id=job_id,
                event_type="IMAGE_READY",
                payload={"image_id": image_id, "instance_count": len(instance_rows)},
            )

        _write_meta_sidecar(
            settings=resources.settings,
            image_id=image_id,
            payload=payload,
            processed=processed,
            pil_image=pil_image,
            raw_path=raw_path,
        )

        return {
            "image_id": image_id,
            "instance_count": len(instance_rows),
            "pipeline_stage": "READY",
        }
    except Exception as exc:
        with db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.update_image_status(
                image_id,
                ingest_status="FAILED",
                pipeline_stage="FAILED",
                last_error_code="INGEST_PIPELINE_ERROR",
                last_error_message=str(exc),
            )
            if request_id is not None:
                repo.update_ingest_request_status(request_id, status="FAILED", image_id=image_id)
            repo.append_job_event(
                job_id=job_id,
                event_type="IMAGE_FAILED",
                payload={"image_id": image_id, "error": str(exc)},
            )
        raise


def _run_gpu_ingest_steps(
    *,
    db: DatabaseManager,
    resources: WorkerResources,
    job_id: uuid.UUID,
    image_id: str,
    pil_image: Image.Image,
    payload: dict[str, Any],
) -> dict[str, Any]:
    settings = resources.settings
    image_role = str(payload.get("image_role") or "DAILY").upper()
    captured_at_raw = payload.get("captured_at")
    captured_at_ts = None
    if captured_at_raw:
        try:
            captured_at_ts = int(datetime.fromisoformat(str(captured_at_raw).replace("Z", "+00:00")).timestamp())
        except Exception:
            captured_at_ts = None
    w, h = pil_image.size

    with db.session_scope() as session:
        repo = ReIdRepository(session)
        repo.update_image_status(image_id, pipeline_stage="DETECTING")
        repo.append_job_event(job_id=job_id, event_type="IMAGE_DETECTING", payload={"image_id": image_id})

    if resources.detector is not None:
        detections = resources.detector.detect(pil_image)
    else:
        detections = [
            type("_D", (), {"class_id": 16, "confidence": 1.0, "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0})
        ]

    detections = _filter_small_detections(settings, detections, image_role=image_role)
    source_detections = [_detection_to_source_meta(settings, d) for d in detections]
    primary_source_detection_index: Optional[int] = None

    if image_role == "SEED" and len(detections) > 1:
        primary_source_detection_index = min(
            range(len(detections)), key=lambda idx: _seed_detection_sort_key(detections[idx])
        )
        detections = [detections[primary_source_detection_index]]
    elif image_role == "SEED" and len(detections) == 1:
        primary_source_detection_index = 0

    with db.session_scope() as session:
        repo = ReIdRepository(session)
        repo.update_image_status(image_id, pipeline_stage="CROPPING")
        repo.append_job_event(job_id=job_id, event_type="IMAGE_CROPPING", payload={"image_id": image_id})

    crops = []
    inst_meta = []
    for d in detections:
        bb = NormalizedBBox(x1=float(d.x1), y1=float(d.y1), x2=float(d.x2), y2=float(d.y2))
        bb = pad_bbox(bb, settings.crop_padding)
        crop = crop_from_bbox(pil_image, bb)
        crops.append(crop)
        inst_meta.append((d, bb))

    with db.session_scope() as session:
        repo = ReIdRepository(session)
        repo.update_image_status(image_id, pipeline_stage="EMBEDDING")
        repo.append_job_event(job_id=job_id, event_type="IMAGE_EMBEDDING", payload={"image_id": image_id})

    embs = resources.embedder.embed_pil_images(crops) if crops else []
    points: list[qm.PointStruct] = []
    meta_instances: list[dict[str, Any]] = []
    model_version = resources.embedder.model_info.model_version

    for i, (d, bb) in enumerate(inst_meta):
        point_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"{image_id}:instance:{i}")
        instance_id = f"ins_{point_uuid}"
        emb_vec = embs[i].tolist() if len(embs) else []
        species = _parse_species(int(d.class_id))

        point_payload = {
            "daycare_id": payload.get("daycare_id"),
            "trainer_id": payload.get("trainer_id"),
            "image_id": image_id,
            "image_role": image_role,
            "captured_at_ts": captured_at_ts,
            "species": species,
            "class_id": int(d.class_id),
            "det_conf": float(d.confidence),
            "bbox": {"x1": bb.x1, "y1": bb.y1, "x2": bb.x2, "y2": bb.y2},
            "embedding_type": "BODY",
            "model_version": model_version,
            "instance_id": instance_id,
        }
        points.append(qm.PointStruct(id=str(point_uuid), vector=emb_vec, payload=point_payload))
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

    return {
        "points": points,
        "instances": meta_instances,
        "source_detections": source_detections,
        "source_detection_count": len(source_detections),
        "primary_source_detection_index": primary_source_detection_index,
        "model_version": model_version,
        "width": w,
        "height": h,
    }


def _write_meta_sidecar(
    *,
    settings: Settings,
    image_id: str,
    payload: dict[str, Any],
    processed: dict[str, Any],
    pil_image: Image.Image,
    raw_path: str,
) -> None:
    base_dir = Path(settings.reid_storage_dir)
    meta_dir = base_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = str(payload.get("thumb_path") or "")
    uploaded_at_raw = payload.get("uploaded_at")
    uploaded_at = _utcnow()
    if uploaded_at_raw:
        try:
            uploaded_at = datetime.fromisoformat(str(uploaded_at_raw).replace("Z", "+00:00"))
        except Exception:
            uploaded_at = _utcnow()
    cap_raw = payload.get("captured_at")
    cap_ts = int(uploaded_at.timestamp())
    if cap_raw:
        try:
            cap_ts = int(datetime.fromisoformat(str(cap_raw).replace("Z", "+00:00")).timestamp())
        except Exception:
            cap_ts = int(uploaded_at.timestamp())
    meta = {
        "image": {
            "image_id": image_id,
            "daycare_id": payload.get("daycare_id"),
            "image_role": payload.get("image_role"),
            "pet_name": payload.get("input_pet_name"),
            "trainer_id": payload.get("trainer_id"),
            "captured_at": cap_raw,
            "uploaded_at": uploaded_at.isoformat(),
            "original_filename": payload.get("original_filename"),
            "captured_at_ts": cap_ts,
            "uploaded_at_ts": cap_ts,
            "width": pil_image.size[0],
            "height": pil_image.size[1],
            "raw_path": str(raw_path),
            "thumb_path": thumb_path,
            "raw_url": f"{settings.api_prefix}/images/{image_id}?variant=raw",
            "thumb_url": f"{settings.api_prefix}/images/{image_id}?variant=thumb",
            "instance_count": len(processed["instances"]),
            "pipeline_version": "yolo26x+miewidv3+poc",
        },
        "instances": processed["instances"],
        "source_detections": processed["source_detections"] if payload.get("image_role") == "SEED" else None,
        "primary_source_detection_index": (
            processed["primary_source_detection_index"] if payload.get("image_role") == "SEED" else None
        ),
    }
    (meta_dir / f"{image_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
