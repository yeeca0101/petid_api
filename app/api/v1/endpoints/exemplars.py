"""관리자용 초기 등록 이미지(exemplar) CRUD 엔드포인트 모듈."""

from __future__ import annotations

import json
import uuid
import re
import shutil
import zipfile
from pathlib import Path
from datetime import datetime, time, timezone
from typing import Dict, Iterable, List, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from qdrant_client.http import models as qm
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.ingest import ingest as ingest_image
from app.core.config import settings
from app.schemas.exemplars import (
    ExemplarCreateRequest,
    ExemplarFolderUploadItemResult,
    ExemplarFolderUploadResponse,
    ExemplarItem,
    ExemplarListResponse,
    ExemplarMoveToDailyRequest,
    ExemplarMoveToDailyResponse,
    ExemplarMutationResponse,
    ExemplarQuickRegisterResponse,
    ExemplarUpdateRequest,
)
from app.utils.pet_registry import allocate_pet_id, ensure_pet_mapping, find_pet_ids_by_name, get_pet_name, read_pet_name_map
from app.utils.timezone import business_tz
from app.vector_db.qdrant_store import PointRecord, QdrantStore

router = APIRouter()

_DUPLICATE_NAME_GUIDE = "이미 존재하는 pet 이름이면 고유 pet_id를 만들기 위해 -2, -3 형식의 suffix가 자동 부여됩니다."
_CONFLICT_MESSAGE = "이미 존재하는 pet 이름입니다. 기존 pet에 추가하거나 다른 이름을 입력하세요."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts_to_dt(ts: Optional[object]) -> Optional[datetime]:
    try:
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _read_meta_safe(image_id: str) -> Optional[dict]:
    image_id_clean = str(image_id or "").strip()
    if not image_id_clean:
        return None
    path = Path(settings.reid_storage_dir) / "meta" / f"{image_id_clean}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _annotation_name_for(file_name: str) -> str:
    p = Path(file_name)
    stem = p.stem or "image"
    return f"{stem}_anno.json"


def _exemplar_annotation_payload(meta: dict, pet_name_map: Dict[str, str]) -> dict:
    image = meta.get("image") or {}
    instances = []
    for inst in meta.get("instances") or []:
        pet_id = str(inst.get("pet_id") or "").strip() or None
        seed_pet_id = str(inst.get("seed_pet_id") or "").strip() or None
        resolved_pet_id = seed_pet_id or pet_id
        name = pet_name_map.get(resolved_pet_id) if resolved_pet_id else None
        bbox = inst.get("bbox") or {}
        instances.append(
            {
                "instance_id": str(inst.get("instance_id") or "").strip() or None,
                "name": name,
                "pet_id": resolved_pet_id,
                "bbox": {
                    "x1": float(bbox.get("x1") or 0.0),
                    "y1": float(bbox.get("y1") or 0.0),
                    "x2": float(bbox.get("x2") or 0.0),
                    "y2": float(bbox.get("y2") or 0.0),
                },
                "assignment_status": str(inst.get("assignment_status") or "ACCEPTED").upper(),
            }
        )

    return {
        "image_id": str(image.get("image_id") or "").strip() or None,
        "img_name": (str(image.get("original_filename") or "").strip() or None),
        "image_role": str(image.get("image_role") or "SEED").upper(),
        "captured_at": image.get("captured_at"),
        "width": int(image.get("width") or 0),
        "height": int(image.get("height") or 0),
        "instances": instances,
    }


def _read_image_name(image_id: Optional[str]) -> Optional[str]:
    image_id_clean = str(image_id or "").strip()
    if not image_id_clean:
        return None
    path = Path(settings.reid_storage_dir) / "meta" / f"{image_id_clean}.json"
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    image = meta.get("image") or {}
    name = str(image.get("original_filename") or "").strip()
    return name or None


def _meta_path(image_id: str) -> Path:
    return Path(settings.reid_storage_dir) / "meta" / f"{image_id}.json"


def _delete_file_if_exists(path_value: Optional[object]) -> None:
    raw = str(path_value or "").strip()
    if not raw:
        return
    path = Path(raw)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        return


def _remove_dir_if_empty(path_value: Optional[object]) -> None:
    raw = str(path_value or "").strip()
    if not raw:
        return
    path = Path(raw)
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except Exception:
        return


def _move_file_if_present(src: Path, dest: Path) -> bool:
    if not src.exists() or src.resolve() == dest.resolve():
        return False
    if dest.exists() and src.resolve() != dest.resolve():
        raise RuntimeError(f"Destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return True


def _restore_moved_instance_meta(rollback: dict) -> None:
    meta_path = Path(str(rollback.get("meta_path") or "").strip())
    original_meta_text = str(rollback.get("original_meta_text") or "")
    old_raw_path = Path(str(rollback.get("old_raw_path") or "").strip()) if str(rollback.get("old_raw_path") or "").strip() else None
    old_thumb_path = Path(str(rollback.get("old_thumb_path") or "").strip()) if str(rollback.get("old_thumb_path") or "").strip() else None
    new_raw_path = Path(str(rollback.get("new_raw_path") or "").strip()) if str(rollback.get("new_raw_path") or "").strip() else None
    new_thumb_path = Path(str(rollback.get("new_thumb_path") or "").strip()) if str(rollback.get("new_thumb_path") or "").strip() else None

    if rollback.get("moved_raw") and old_raw_path is not None and new_raw_path is not None and new_raw_path.exists():
        old_raw_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_raw_path), str(old_raw_path))
    if rollback.get("moved_thumb") and old_thumb_path is not None and new_thumb_path is not None and new_thumb_path.exists():
        old_thumb_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new_thumb_path), str(old_thumb_path))

    if meta_path and original_meta_text:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(original_meta_text, encoding="utf-8")

    if new_raw_path is not None:
        _remove_dir_if_empty(new_raw_path.parent)
    if new_thumb_path is not None:
        _remove_dir_if_empty(new_thumb_path.parent)


def _business_daily_folder_from_ts(ts: Optional[int]) -> str:
    if ts is None:
        return datetime.now(timezone.utc).astimezone(business_tz()).date().isoformat()
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(business_tz()).date().isoformat()


def _build_daily_instances_from_source_detections(
    source_detections: List[dict],
    primary_index: Optional[int],
    primary_instance_id: str,
    assignment_status: Literal["UNREVIEWED", "ACCEPTED"],
    pet_id: Optional[str],
    updated_by: Optional[str],
    now_ts: int,
) -> List[dict]:
    if not source_detections:
        return []

    resolved_primary_index = 0
    if primary_index is not None and 0 <= int(primary_index) < len(source_detections):
        resolved_primary_index = int(primary_index)

    items: List[dict] = []
    for idx, det in enumerate(source_detections):
        bbox = det.get("bbox") or {}
        is_primary = idx == resolved_primary_index
        inst_assignment_status = assignment_status if is_primary else "UNREVIEWED"
        inst_pet_id = pet_id if is_primary and assignment_status == "ACCEPTED" else None
        items.append(
            {
                "instance_id": primary_instance_id if is_primary else f"ins_{uuid.uuid4()}",
                "class_id": int(det.get("class_id") or 0),
                "species": str(det.get("species") or "UNKNOWN"),
                "confidence": float(det.get("confidence") or 0.0),
                "bbox": {
                    "x1": float(bbox.get("x1") or 0.0),
                    "y1": float(bbox.get("y1") or 0.0),
                    "x2": float(bbox.get("x2") or 0.0),
                    "y2": float(bbox.get("y2") or 0.0),
                },
                "pet_id": inst_pet_id,
                "assignment_status": inst_assignment_status,
                "label_source": "MANUAL" if inst_assignment_status == "ACCEPTED" else None,
                "label_confidence": 1.0 if inst_assignment_status == "ACCEPTED" else None,
                "labeled_at_ts": now_ts if inst_assignment_status == "ACCEPTED" else None,
                "labeled_by": updated_by if inst_assignment_status == "ACCEPTED" else None,
            }
        )
    return items


def _move_instance_to_daily_meta(
    instance_id: str,
    image_id: Optional[str],
    assignment_status: Literal["UNREVIEWED", "ACCEPTED"],
    pet_id: Optional[str],
    updated_by: Optional[str],
    now_ts: int,
    target_captured_at_iso: Optional[str],
    target_captured_at_ts: Optional[int],
) -> dict:
    image_id_clean = str(image_id or "").strip()
    if not image_id_clean:
        raise RuntimeError("image_id missing for move-to-daily")

    meta_path = _meta_path(image_id_clean)
    if not meta_path.exists():
        raise FileNotFoundError(f"Image meta not found for move-to-daily: {image_id_clean}")

    original_meta_text = meta_path.read_text(encoding="utf-8")
    try:
        meta = json.loads(original_meta_text)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse meta for move-to-daily: {image_id_clean}") from exc

    image = meta.get("image") or {}
    base_dir = Path(settings.reid_storage_dir)
    old_raw_path = Path(str(image.get("raw_path") or "").strip()) if str(image.get("raw_path") or "").strip() else None
    old_thumb_path = Path(str(image.get("thumb_path") or "").strip()) if str(image.get("thumb_path") or "").strip() else None
    old_raw_parent = old_raw_path.parent if old_raw_path is not None else None
    old_thumb_parent = old_thumb_path.parent if old_thumb_path is not None else None

    ext = ".jpg"
    if old_raw_path is not None and old_raw_path.suffix:
        ext = old_raw_path.suffix.lower()
    target_folder = _business_daily_folder_from_ts(target_captured_at_ts or image.get("captured_at_ts") or image.get("uploaded_at_ts"))
    new_raw_path = base_dir / "images" / "daily" / target_folder / f"{image_id_clean}{ext}"
    new_thumb_path = base_dir / "thumbs" / "daily" / target_folder / f"{image_id_clean}.jpg"

    source_detections = list(meta.get("source_detections") or [])
    primary_source_detection_index = meta.get("primary_source_detection_index")
    changed = False
    if source_detections:
        meta["instances"] = _build_daily_instances_from_source_detections(
            source_detections=source_detections,
            primary_index=(int(primary_source_detection_index) if primary_source_detection_index is not None else None),
            primary_instance_id=instance_id,
            assignment_status=assignment_status,
            pet_id=pet_id,
            updated_by=updated_by,
            now_ts=now_ts,
        )
        image["instance_count"] = len(meta.get("instances") or [])
        changed = True
    else:
        for inst in meta.get("instances") or []:
            if str(inst.get("instance_id") or "") != instance_id:
                continue
            inst["pet_id"] = pet_id
            inst["assignment_status"] = assignment_status
            inst["label_source"] = "MANUAL" if assignment_status == "ACCEPTED" else None
            inst["label_confidence"] = 1.0 if assignment_status == "ACCEPTED" else None
            inst["labeled_at_ts"] = now_ts if assignment_status == "ACCEPTED" else None
            inst["labeled_by"] = updated_by if assignment_status == "ACCEPTED" else None
            changed = True
            break
    if not changed:
        raise RuntimeError(f"instance not found in meta for move-to-daily: {instance_id}")

    image["image_role"] = "DAILY"
    image["raw_path"] = str(new_raw_path)
    image["thumb_path"] = str(new_thumb_path)
    image["raw_url"] = f"{settings.api_prefix}/images/{image_id_clean}?variant=raw"
    image["thumb_url"] = f"{settings.api_prefix}/images/{image_id_clean}?variant=thumb"
    if target_captured_at_iso is not None:
        image["captured_at"] = target_captured_at_iso
    if target_captured_at_ts is not None:
        image["captured_at_ts"] = target_captured_at_ts
    meta["image"] = image

    moved_raw = False
    moved_thumb = False
    try:
        if old_raw_path is not None:
            moved_raw = _move_file_if_present(old_raw_path, new_raw_path)
        if old_thumb_path is not None:
            moved_thumb = _move_file_if_present(old_thumb_path, new_thumb_path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        rollback = {
            "meta_path": str(meta_path),
            "original_meta_text": original_meta_text,
            "old_raw_path": str(old_raw_path) if old_raw_path is not None else None,
            "old_thumb_path": str(old_thumb_path) if old_thumb_path is not None else None,
            "new_raw_path": str(new_raw_path),
            "new_thumb_path": str(new_thumb_path),
            "moved_raw": moved_raw,
            "moved_thumb": moved_thumb,
        }
        _restore_moved_instance_meta(rollback)
        raise

    _remove_dir_if_empty(old_raw_parent)
    _remove_dir_if_empty(old_thumb_parent)

    return {
        "image_id": str(image.get("image_id") or image_id_clean),
        "updated": changed,
        "assignment_status": assignment_status,
        "pet_id": pet_id,
        "raw_path": str(new_raw_path),
        "thumb_path": str(new_thumb_path),
        "rollback": {
            "meta_path": str(meta_path),
            "original_meta_text": original_meta_text,
            "old_raw_path": str(old_raw_path) if old_raw_path is not None else None,
            "old_thumb_path": str(old_thumb_path) if old_thumb_path is not None else None,
            "new_raw_path": str(new_raw_path),
            "new_thumb_path": str(new_thumb_path),
            "moved_raw": moved_raw,
            "moved_thumb": moved_thumb,
        },
    }


def _remove_instance_from_meta_and_maybe_delete_assets(instance_id: str, image_id: Optional[str]) -> dict:
    image_id_clean = str(image_id or "").strip()
    if not image_id_clean:
        return {}

    meta_path = _meta_path(image_id_clean)
    if not meta_path.exists():
        return {"image_id": image_id_clean, "remaining_instances": 0, "deleted_files": False}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {"image_id": image_id_clean, "remaining_instances": 0, "deleted_files": False}

    image = meta.get("image") or {}
    instances = list(meta.get("instances") or [])
    kept = [inst for inst in instances if str(inst.get("instance_id") or "") != instance_id]
    if len(kept) == len(instances):
        return {
            "image_id": str(image.get("image_id") or image_id_clean),
            "remaining_instances": len(instances),
            "deleted_files": False,
        }

    remaining = len(kept)
    if remaining > 0:
        image["instance_count"] = remaining
        meta["image"] = image
        meta["instances"] = kept
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {
            "image_id": str(image.get("image_id") or image_id_clean),
            "remaining_instances": remaining,
            "deleted_files": False,
        }

    _delete_file_if_exists(image.get("raw_path"))
    _delete_file_if_exists(image.get("thumb_path"))
    try:
        meta_path.unlink(missing_ok=True)
    except TypeError:
        if meta_path.exists():
            meta_path.unlink()
    return {
        "image_id": str(image.get("image_id") or image_id_clean),
        "remaining_instances": 0,
        "deleted_files": True,
    }


def _normalize_ids(instance_ids: Iterable[str]) -> List[str]:
    dedup = []
    seen = set()
    for iid in instance_ids:
        if iid in seen:
            continue
        seen.add(iid)
        dedup.append(iid)
    return dedup


def _to_exemplar_item(store: QdrantStore, p: PointRecord) -> ExemplarItem:
    payload = p.payload or {}
    instance_id = payload.get("instance_id")
    if not instance_id:
        instance_id = store.external_instance_id(p.point_id)

    return ExemplarItem(
        instance_id=str(instance_id),
        image_id=(str(payload.get("image_id")) if payload.get("image_id") is not None else None),
        img_name=_read_image_name(payload.get("image_id")),
        species=(str(payload.get("species")) if payload.get("species") is not None else None),
        pet_id=str(payload.get("seed_pet_id") or ""),
        active=bool(payload.get("seed_active", True)),
        rank=(int(payload["seed_rank"]) if payload.get("seed_rank") is not None else None),
        note=(str(payload.get("seed_note")) if payload.get("seed_note") is not None else None),
        created_at=_ts_to_dt(payload.get("seed_created_at_ts")),
        created_by=(str(payload.get("seed_created_by")) if payload.get("seed_created_by") is not None else None),
        updated_at=_ts_to_dt(payload.get("seed_updated_at_ts")),
        updated_by=(str(payload.get("seed_updated_by")) if payload.get("seed_updated_by") is not None else None),
        synced_label_pet_id=(str(payload.get("pet_id")) if payload.get("pet_id") is not None else None),
        synced_assignment_status=(
            str(payload.get("assignment_status")) if payload.get("assignment_status") is not None else None
        ),
    )


def _safe_archive_name(name: Optional[str], default: str = "unknown") -> str:
    raw = (name or "").strip()
    if not raw:
        raw = default
    safe = re.sub(r'[\/:*?"<>|]+', '_', raw)
    safe = safe.replace('..', '_').strip().strip('.')
    return safe or default


def _zip_temp_path(prefix: str) -> Path:
    export_dir = Path(settings.reid_storage_dir) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir / f"{prefix}_{int(_utcnow().timestamp())}.zip"


def _seed_filter(pet_id: Optional[str], active: Optional[bool]) -> qm.Filter:
    must: List[qm.FieldCondition] = [
        qm.FieldCondition(key="is_seed", match=qm.MatchValue(value=True)),
    ]
    if pet_id:
        must.append(qm.FieldCondition(key="seed_pet_id", match=qm.MatchValue(value=pet_id)))
    if active is not None:
        must.append(qm.FieldCondition(key="seed_active", match=qm.MatchValue(value=active)))
    return qm.Filter(must=must)


def _pet_id_from_name(pet_name: str) -> str:
    pet_name = pet_name.strip()
    if not pet_name:
        raise HTTPException(status_code=400, detail="pet_name is required")
    try:
        return allocate_pet_id(pet_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _pet_name_from_relative_path(relative_path: str) -> str:
    parts = [p for p in str(relative_path).replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid relative path: {relative_path}")
    if len(parts) == 2:
        return parts[0]
    return parts[1]


async def _register_exemplar_from_uploaded_file(
    request: Request,
    store: QdrantStore,
    file: UploadFile,
    pet_id: Optional[str],
    pet_name: str,
    updated_by: Optional[str],
    trainer_id: Optional[str],
    captured_at: Optional[str],
    sync_label: bool,
    apply_to_all_instances: bool,
) -> tuple[str, str, str, List[ExemplarItem]]:
    pet_name_clean = pet_name.strip()
    pet_id_clean = str(pet_id or "").strip()
    now = _utcnow()
    now_ts = int(now.timestamp())

    ingest_resp = await ingest_image(
        request=request,
        file=file,
        daycare_id=None,
        trainer_id=trainer_id,
        captured_at=captured_at,
        image_role="SEED",
        pet_name=pet_name_clean,
        include_embedding=False,
    )
    instances = list(ingest_resp.instances or [])
    if not instances:
        raise HTTPException(status_code=400, detail="No detected instances in uploaded image")

    if not pet_id_clean:
        pet_id_clean = _pet_id_from_name(pet_name_clean)
    ensure_pet_mapping(pet_id_clean, pet_name_clean)

    selected = instances if apply_to_all_instances else [max(instances, key=lambda x: float(x.confidence))]

    updates: Dict[str, dict] = {}
    for inst in selected:
        payload = {
            "is_seed": True,
            "seed_pet_id": pet_id_clean,
            "seed_active": True,
            "seed_rank": None,
            "seed_note": "quick_upload",
            "seed_created_at_ts": now_ts,
            "seed_created_by": updated_by,
            "seed_updated_at_ts": now_ts,
            "seed_updated_by": updated_by,
            "pet_name": pet_name_clean,
        }
        if sync_label:
            payload.update(
                {
                    "pet_id": pet_id_clean,
                    "assignment_status": "ACCEPTED",
                    "label_source": "MANUAL",
                    "label_confidence": 1.0,
                    "labeled_at_ts": now_ts,
                    "labeled_by": updated_by,
                }
            )
        updates[str(inst.instance_id)] = payload

    for instance_id, payload in updates.items():
        await run_in_threadpool(store.set_payload, [instance_id], payload)

    updated_points = await run_in_threadpool(store.retrieve_points, updates.keys(), False)
    items = [_to_exemplar_item(store, p) for p in updated_points.values()]
    items.sort(key=lambda x: x.instance_id)
    return pet_id_clean, pet_name_clean, str(ingest_resp.image.image_id), items


def _resolve_folder_upload_target(pet_name: str, existing_name_policy: Literal["append", "create_new", "fail"]) -> tuple[str, str]:
    pet_name_clean = str(pet_name or "").strip()
    if not pet_name_clean:
        raise HTTPException(status_code=400, detail="pet_name is required")

    conflicts = find_pet_ids_by_name(pet_name_clean)
    if existing_name_policy == "create_new":
        return _pet_id_from_name(pet_name_clean), pet_name_clean

    if not conflicts:
        return _pet_id_from_name(pet_name_clean), pet_name_clean

    if existing_name_policy == "fail":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PET_NAME_CONFLICT",
                "message": "이미 존재하는 pet 이름입니다. 다른 정책을 선택하거나 이름을 바꿔주세요.",
                "pet_name": pet_name_clean,
                "existing_pet_ids": conflicts,
            },
        )

    exact = next((pet_id for pet_id in conflicts if pet_id == pet_name_clean), None)
    if exact:
        return exact, get_pet_name(exact) or pet_name_clean
    if len(conflicts) == 1:
        only = conflicts[0]
        return only, get_pet_name(only) or pet_name_clean

    raise HTTPException(
        status_code=409,
        detail={
            "code": "PET_NAME_AMBIGUOUS",
            "message": "같은 이름의 기존 pet이 여러 개 있어 자동으로 추가할 수 없습니다. 새 pet 생성 또는 등록 중단 정책을 사용하세요.",
            "pet_name": pet_name_clean,
            "existing_pet_ids": conflicts,
        },
    )


def _resolve_quick_upload_target(pet_id: Optional[str], pet_name: Optional[str]) -> tuple[str, Optional[str], str]:
    pet_id_clean = str(pet_id or "").strip()
    pet_name_clean = str(pet_name or "").strip()

    if bool(pet_id_clean) == bool(pet_name_clean):
        raise HTTPException(status_code=400, detail="Provide exactly one of pet_id or pet_name")

    if pet_id_clean:
        existing_name = get_pet_name(pet_id_clean)
        if not existing_name:
            raise HTTPException(status_code=404, detail="pet_id not found")
        return "append", pet_id_clean, existing_name

    conflicts = find_pet_ids_by_name(pet_name_clean)
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PET_NAME_CONFLICT",
                "message": _CONFLICT_MESSAGE,
                "pet_name": pet_name_clean,
                "existing_pet_ids": conflicts,
            },
        )

    return "create", None, pet_name_clean


@router.get("/exemplars", response_model=ExemplarListResponse)
async def list_exemplars(
    request: Request,
    pet_id: Optional[str] = Query(default=None),
    species: Optional[str] = Query(default=None),
    active: Optional[bool] = Query(default=True),
    q: Optional[str] = Query(default=None, description="instance_id/image_id/note 부분 검색"),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
):
    store = _get_store(request)
    points = await run_in_threadpool(store.scroll_points, _seed_filter(pet_id, active), 1000, False)
    pet_name_map = read_pet_name_map()

    items = [_to_exemplar_item(store, p) for p in points]
    if species:
        species_upper = species.upper()
        items = [x for x in items if (x.species or "").upper() == species_upper]
    if q:
        needle = q.lower().strip()
        items = [
            x
            for x in items
            if (needle in x.instance_id.lower())
            or (needle in (x.image_id or "").lower())
            or (needle in (x.note or "").lower())
            or (needle in x.pet_id.lower())
            or (needle in pet_name_map.get(x.pet_id, "").lower())
        ]

    items.sort(
        key=lambda x: (
            -int(x.active),
            x.rank if x.rank is not None else 999999,
            (x.updated_at or datetime.fromtimestamp(0, tz=timezone.utc)),
        ),
        reverse=False,
    )
    sliced = items[offset : offset + limit]
    return ExemplarListResponse(count=len(sliced), items=sliced)


@router.post("/exemplars", response_model=ExemplarMutationResponse)
async def create_exemplars(request: Request, body: ExemplarCreateRequest):
    store = _get_store(request)
    now = _utcnow()
    now_ts = int(now.timestamp())

    raw_instance_ids = _normalize_ids([x.instance_id for x in body.items])
    instance_ids = [store.external_instance_id(x) for x in raw_instance_ids]
    points = await run_in_threadpool(store.retrieve_points, raw_instance_ids, False)
    if len(points) != len(instance_ids):
        missing = sorted(set(instance_ids) - set(points.keys()))
        raise HTTPException(status_code=404, detail=f"instance_ids not found: {missing}")

    updates: Dict[str, dict] = {}
    for item in body.items:
        key = store.external_instance_id(item.instance_id)
        p = points.get(key)
        if p is None:
            continue

        ensure_pet_mapping(item.pet_id, item.pet_id)
        payload = p.payload or {}
        new_payload = {
            "is_seed": True,
            "seed_pet_id": item.pet_id,
            "seed_active": bool(item.active),
            "seed_rank": item.rank,
            "seed_note": item.note,
            "seed_created_at_ts": int(payload.get("seed_created_at_ts") or now_ts),
            "seed_created_by": payload.get("seed_created_by") or body.updated_by,
            "seed_updated_at_ts": now_ts,
            "seed_updated_by": body.updated_by,
        }
        if item.sync_label:
            new_payload.update(
                {
                    "pet_id": item.pet_id,
                    "assignment_status": "ACCEPTED",
                    "label_source": "MANUAL",
                    "label_confidence": 1.0,
                    "labeled_at_ts": now_ts,
                    "labeled_by": body.updated_by,
                }
            )
        updates[key] = new_payload

    for instance_id, payload in updates.items():
        await run_in_threadpool(store.set_payload, [instance_id], payload)

    updated_points = await run_in_threadpool(store.retrieve_points, updates.keys(), False)
    items = [_to_exemplar_item(store, p) for p in updated_points.values()]
    items.sort(key=lambda x: x.instance_id)
    return ExemplarMutationResponse(updated_at=now, count=len(items), items=items)


@router.get("/exemplars/zip")
async def download_exemplars_zip(
    request: Request,
    root_folder_name: Optional[str] = Query(default=None, description="Archive root folder name"),
):
    store = _get_store(request)
    pet_name_map = read_pet_name_map()
    points = await run_in_threadpool(store.scroll_points, _seed_filter(pet_id=None, active=True), 1000, False)
    root_name = _safe_archive_name(root_folder_name, "exemplars")
    zip_path = _zip_temp_path("exemplars")

    written = 0
    used_paths = set()
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for point in points:
            payload = point.payload or {}
            image_id = str(payload.get("image_id") or "").strip()
            if not image_id:
                continue
            meta = _read_meta_safe(image_id)
            if meta is None:
                continue
            image = meta.get("image") or {}
            raw_path = Path(str(image.get("raw_path") or ""))
            if not raw_path.exists():
                continue
            pet_id = str(payload.get("seed_pet_id") or "").strip() or "unknown"
            pet_folder = _safe_archive_name(pet_name_map.get(pet_id) or pet_id, pet_id)
            base_name = str(image.get("original_filename") or "").strip() or raw_path.name
            file_name = _safe_archive_name(base_name, raw_path.name)
            arcname = f"{root_name}/{pet_folder}/{file_name}"
            if arcname in used_paths:
                file_name = _safe_archive_name(f"{image_id}_{base_name}", f"{image_id}_{raw_path.name}")
                arcname = f"{root_name}/{pet_folder}/{file_name}"
            used_paths.add(arcname)
            zf.write(raw_path, arcname)

            anno_name = _annotation_name_for(file_name)
            anno_arcname = f"{root_name}/{pet_folder}/{anno_name}"
            if anno_arcname in used_paths:
                anno_name = _annotation_name_for(f"{image_id}_{file_name}")
                anno_arcname = f"{root_name}/{pet_folder}/{anno_name}"
            used_paths.add(anno_arcname)
            anno_payload = _exemplar_annotation_payload(meta, pet_name_map)
            zf.writestr(anno_arcname, json.dumps(anno_payload, ensure_ascii=False, indent=2))
            written += 1

    if written == 0:
        raise HTTPException(status_code=404, detail="No exemplar image files available for zip export")

    return FileResponse(
        path=zip_path,
        media_type='application/zip',
        filename=f"{root_name}.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )


@router.post("/exemplars/upload", response_model=ExemplarQuickRegisterResponse)
async def upload_exemplar_quick(
    request: Request,
    file: UploadFile = File(...),
    pet_id: Optional[str] = Form(default=None),
    pet_name: Optional[str] = Form(default=None),
    updated_by: Optional[str] = Form(default=None),
    trainer_id: Optional[str] = Form(default=None),
    captured_at: Optional[str] = Form(default=None),
    sync_label: bool = Form(default=True),
    apply_to_all_instances: bool = Form(default=False),
):
    """Quick admin flow: upload one seed image either for a new pet or append it to an existing pet."""
    now = _utcnow()
    store = _get_store(request)
    mode, resolved_pet_id, resolved_pet_name, image_id, items = None, None, None, None, None
    mode, resolved_pet_id, resolved_pet_name = _resolve_quick_upload_target(pet_id, pet_name)
    resolved_pet_id, resolved_pet_name, image_id, items = await _register_exemplar_from_uploaded_file(
        request=request,
        store=store,
        file=file,
        pet_id=resolved_pet_id,
        pet_name=resolved_pet_name,
        updated_by=updated_by,
        trainer_id=trainer_id,
        captured_at=captured_at,
        sync_label=sync_label,
        apply_to_all_instances=apply_to_all_instances,
    )

    return ExemplarQuickRegisterResponse(
        mode=mode,
        pet_id=resolved_pet_id,
        pet_name=resolved_pet_name,
        image_id=image_id,
        updated_at=now,
        count=len(items),
        items=items,
        message=("기존 pet에 seed exemplar를 추가했습니다." if mode == "append" else "새 pet seed exemplar를 등록했습니다."),
    )


@router.post("/exemplars/upload-folder", response_model=ExemplarFolderUploadResponse)
async def upload_exemplar_folder(
    request: Request,
    files: List[UploadFile] = File(...),
    relative_paths: List[str] = Form(...),
    updated_by: Optional[str] = Form(default=None),
    trainer_id: Optional[str] = Form(default=None),
    captured_at: Optional[str] = Form(default=None),
    sync_label: bool = Form(default=True),
    apply_to_all_instances: bool = Form(default=False),
    existing_name_policy: Literal["append", "create_new", "fail"] = Form(default="append"),
    skip_on_error: bool = Form(default=True),
):
    """Batch folder upload for admin dashboard.

    Expected structure:
    - root/pet_name/image.ext
    - pet_name/image.ext
    """
    if len(files) != len(relative_paths):
        raise HTTPException(status_code=400, detail="files and relative_paths count mismatch")

    store = _get_store(request)
    now = _utcnow()
    results: List[ExemplarFolderUploadItemResult] = []
    succeeded = 0
    failed = 0
    resolved_pet_targets: Dict[str, tuple[str, str]] = {}

    for upload, rel_path in zip(files, relative_paths):
        try:
            pet_name = _pet_name_from_relative_path(rel_path)
            target = resolved_pet_targets.get(pet_name)
            if not target:
                target = _resolve_folder_upload_target(pet_name, existing_name_policy)
                resolved_pet_targets[pet_name] = target
            pet_id, resolved_pet_name = target
            pet_id, _pet_name_clean, image_id, items = await _register_exemplar_from_uploaded_file(
                request=request,
                store=store,
                file=upload,
                pet_id=pet_id,
                pet_name=resolved_pet_name,
                updated_by=updated_by,
                trainer_id=trainer_id,
                captured_at=captured_at,
                sync_label=sync_label,
                apply_to_all_instances=apply_to_all_instances,
            )
            succeeded += 1
            results.append(
                ExemplarFolderUploadItemResult(
                    relative_path=rel_path,
                    pet_name=resolved_pet_name,
                    pet_id=pet_id,
                    image_id=image_id,
                    img_name=(upload.filename or None),
                    registered_instances=len(items),
                    status="ok",
                )
            )
        except Exception as e:
            failed += 1
            results.append(
                ExemplarFolderUploadItemResult(
                    relative_path=rel_path,
                    pet_name=(_pet_name_from_relative_path(rel_path) if rel_path else None),
                    status="failed",
                    error=str(e),
                )
            )
            if not skip_on_error:
                raise HTTPException(status_code=400, detail=f"Failed at {rel_path}: {e}") from e

    return ExemplarFolderUploadResponse(
        updated_at=now,
        total_files=len(files),
        succeeded=succeeded,
        failed=failed,
        results=results,
        message=f"중복 이름 정책: {existing_name_policy}",
    )


@router.patch("/exemplars/{instance_id}", response_model=ExemplarMutationResponse)
async def update_exemplar(request: Request, instance_id: str, body: ExemplarUpdateRequest):
    store = _get_store(request)
    now = _utcnow()
    now_ts = int(now.timestamp())

    points = await run_in_threadpool(store.retrieve_points, [instance_id], False)
    key = store.external_instance_id(instance_id)
    point = points.get(key)
    if point is None:
        raise HTTPException(status_code=404, detail="instance not found")

    payload = point.payload or {}
    if not bool(payload.get("is_seed", False)):
        raise HTTPException(status_code=400, detail="instance is not an exemplar")

    patch: Dict[str, object] = {
        "seed_updated_at_ts": now_ts,
        "seed_updated_by": body.updated_by,
    }
    if body.pet_id is not None:
        ensure_pet_mapping(body.pet_id, body.pet_id)
        patch["seed_pet_id"] = body.pet_id
    if body.note is not None:
        patch["seed_note"] = body.note
    if body.clear_note:
        patch["seed_note"] = None
    if body.rank is not None:
        patch["seed_rank"] = body.rank
    if body.active is not None:
        patch["seed_active"] = body.active
    if body.sync_label and body.pet_id is not None:
        patch.update(
            {
                "pet_id": body.pet_id,
                "assignment_status": "ACCEPTED",
                "label_source": "MANUAL",
                "label_confidence": 1.0,
                "labeled_at_ts": now_ts,
                "labeled_by": body.updated_by,
            }
        )

    await run_in_threadpool(store.set_payload, [key], patch)
    updated = await run_in_threadpool(store.retrieve_points, [key], False)
    item = updated.get(key)
    if item is None:
        raise HTTPException(status_code=404, detail="instance not found after update")
    exemplar = _to_exemplar_item(store, item)
    return ExemplarMutationResponse(updated_at=now, count=1, items=[exemplar])


@router.post("/exemplars/{instance_id}/move-to-daily", response_model=ExemplarMoveToDailyResponse)
async def move_exemplar_to_daily(request: Request, instance_id: str, body: ExemplarMoveToDailyRequest):
    store = _get_store(request)
    now = _utcnow()
    now_ts = int(now.timestamp())

    points = await run_in_threadpool(store.retrieve_points, [instance_id], False)
    key = store.external_instance_id(instance_id)
    point = points.get(key)
    if point is None:
        raise HTTPException(status_code=404, detail="instance not found")

    payload = point.payload or {}
    if not bool(payload.get("is_seed", False)):
        raise HTTPException(status_code=400, detail="instance is not an exemplar")

    source_pet_id = str(payload.get("seed_pet_id") or "").strip() or None
    assignment_status = "UNREVIEWED"
    target_pet_id = None
    target_captured_at_iso = None
    target_captured_at_ts = None
    if body.target_date is not None:
        local_dt = datetime.combine(body.target_date, time(hour=12), tzinfo=business_tz())
        utc_dt = local_dt.astimezone(timezone.utc)
        target_captured_at_iso = utc_dt.isoformat()
        target_captured_at_ts = int(utc_dt.timestamp())
    patch: Dict[str, object] = {
        "is_seed": False,
        "seed_pet_id": None,
        "seed_active": None,
        "seed_rank": None,
        "seed_note": None,
        "seed_updated_at_ts": now_ts,
        "seed_updated_by": body.updated_by,
        "image_role": "DAILY",
        "captured_at_ts": target_captured_at_ts if target_captured_at_ts is not None else payload.get("captured_at_ts"),
    }

    if body.mode == "ACCEPTED":
        if not source_pet_id:
            raise HTTPException(status_code=400, detail="seed_pet_id not found for exemplar")
        assignment_status = "ACCEPTED"
        target_pet_id = source_pet_id
        patch.update(
            {
                "pet_id": target_pet_id,
                "assignment_status": assignment_status,
                "label_source": "MANUAL",
                "label_confidence": 1.0,
                "labeled_at_ts": now_ts,
                "labeled_by": body.updated_by,
            }
        )
    else:
        patch.update(
            {
                "pet_id": None,
                "assignment_status": "UNREVIEWED",
                "label_source": None,
                "label_confidence": None,
                "labeled_at_ts": None,
                "labeled_by": None,
            }
        )

    move_result = await run_in_threadpool(
        _move_instance_to_daily_meta,
        key,
        str(payload.get("image_id") or "").strip() or None,
        assignment_status,
        target_pet_id,
        body.updated_by,
        now_ts,
        target_captured_at_iso,
        target_captured_at_ts,
    )
    try:
        await run_in_threadpool(store.set_payload, [key], patch)
    except Exception:
        rollback = move_result.get("rollback") if isinstance(move_result, dict) else None
        if rollback:
            try:
                await run_in_threadpool(_restore_moved_instance_meta, rollback)
            except Exception:
                pass
        raise

    return ExemplarMoveToDailyResponse(
        instance_id=key,
        image_id=(str(payload.get("image_id") or "").strip() or None),
        assignment_status=assignment_status,
        pet_id=target_pet_id,
        updated_at=now,
    )


@router.delete("/exemplars/{instance_id}", response_model=ExemplarMutationResponse)
async def delete_exemplar(
    request: Request,
    instance_id: str,
    updated_by: Optional[str] = Query(default=None),
):
    store = _get_store(request)
    now = _utcnow()

    points = await run_in_threadpool(store.retrieve_points, [instance_id], False)
    key = store.external_instance_id(instance_id)
    point = points.get(key)
    if point is None:
        raise HTTPException(status_code=404, detail="instance not found")

    payload = point.payload or {}
    if not bool(payload.get("is_seed", False)):
        raise HTTPException(status_code=400, detail="instance is not an exemplar")

    image_id = str(payload.get("image_id") or "").strip() or None

    await run_in_threadpool(store.delete_points, [key])
    await run_in_threadpool(_remove_instance_from_meta_and_maybe_delete_assets, key, image_id)

    return ExemplarMutationResponse(updated_at=now, count=0, items=[])
