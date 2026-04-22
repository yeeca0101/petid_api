"""저장된 이미지 목록/원본/메타를 제공하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import re
import zipfile
import mimetypes
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional
from app.utils.timezone import business_tz

from fastapi import APIRouter, HTTPException, Query, Request
from qdrant_client.http import models as qm
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db.models.reid import ImageRecord
from app.db.repositories import ReIdRepository
from app.schemas.images import CalendarDayCountItem, GalleryImageItem, ImageDeleteResponse, ImageMetaResponse, ImagesCalendarResponse, ImagesListResponse
from app.schemas.ingest import BBox, InstanceOut
from app.vector_db.qdrant_store import QdrantStore, build_filter
from app.utils.pet_registry import read_pet_name_map

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


def _meta_path(image_id: str) -> Path:
    return Path(settings.reid_storage_dir) / "meta" / f"{image_id}.json"


def _read_meta(image_id: str) -> dict:
    p = _meta_path(image_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Image meta not found")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse meta: {e}") from e


def _build_item(meta: dict) -> GalleryImageItem:
    img = meta.get("image") or {}
    instances = meta.get("instances") or []
    image_role = str(img.get("image_role") or "DAILY").upper()
    if image_role not in ("DAILY", "SEED"):
        image_role = "DAILY"
    pet_ids = sorted(
        {
            str(i.get("pet_id") or "").strip()
            for i in instances
            if str(i.get("assignment_status") or "").upper() == "ACCEPTED" and str(i.get("pet_id") or "").strip()
        }
    )
    return GalleryImageItem(
        image_id=str(img.get("image_id")),
        image_role=image_role,
        trainer_id=img.get("trainer_id"),
        captured_at=img.get("captured_at"),
        uploaded_at=img.get("uploaded_at"),
        width=int(img.get("width") or 0),
        height=int(img.get("height") or 0),
        raw_url=str(img.get("raw_url") or ""),
        thumb_url=str(img.get("thumb_url") or ""),
        img_name=(str(img.get("original_filename") or "").strip() or None),
        instance_count=int(img.get("instance_count") or len(instances)),
        pet_ids=pet_ids,
    )


def _read_meta_safe(image_id: str) -> Optional[dict]:
    p = _meta_path(image_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _delete_file_if_exists(path_value: Optional[object]) -> bool:
    raw = str(path_value or "").strip()
    if not raw:
        return False
    path = Path(raw)
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:
        return False
    return False


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


def _delete_daily_image_assets(image_id: str, meta: dict) -> bool:
    image = meta.get("image") or {}
    raw_path = Path(str(image.get("raw_path") or "").strip()) if str(image.get("raw_path") or "").strip() else None
    thumb_path = Path(str(image.get("thumb_path") or "").strip()) if str(image.get("thumb_path") or "").strip() else None
    meta_path = _meta_path(image_id)

    deleted_any = False
    if raw_path is not None:
        deleted_any = _delete_file_if_exists(raw_path) or deleted_any
    if thumb_path is not None:
        deleted_any = _delete_file_if_exists(thumb_path) or deleted_any
    if meta_path.exists():
        try:
            meta_path.unlink()
            deleted_any = True
        except Exception:
            pass

    if raw_path is not None:
        _remove_dir_if_empty(raw_path.parent)
    if thumb_path is not None:
        _remove_dir_if_empty(thumb_path.parent)
    return deleted_any


def _instance_ids_for_image(meta: dict) -> List[str]:
    out: List[str] = []
    for inst in meta.get("instances") or []:
        iid = str(inst.get("instance_id") or "").strip()
        if iid:
            out.append(iid)
    return out


def _is_unclassified(meta: dict) -> bool:
    instances = meta.get("instances") or []
    if not instances:
        return True
    for i in instances:
        if (i.get("assignment_status") == "ACCEPTED") and i.get("pet_id"):
            continue
        return True
    return False


def _is_seed_image(meta: dict) -> bool:
    img = meta.get("image") or {}
    return str(img.get("image_role") or "DAILY").upper() == "SEED"


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
    return export_dir / f"{prefix}_{int(datetime.now(timezone.utc).timestamp())}.zip"


def _annotation_name_for(file_name: str) -> str:
    p = Path(file_name)
    stem = p.stem or "image"
    return f"{stem}_anno.json"


def _daily_annotation_payload(meta: dict, pet_name_map: Dict[str, str]) -> dict:
    image = meta.get("image") or {}
    instances = []
    for inst in meta.get("instances") or []:
        pet_id = str(inst.get("pet_id") or "").strip() or None
        name = pet_name_map.get(pet_id) if pet_id else None
        bbox = inst.get("bbox") or {}
        instances.append(
            {
                "instance_id": str(inst.get("instance_id") or "").strip() or None,
                "name": name,
                "pet_id": pet_id,
                "bbox": {
                    "x1": float(bbox.get("x1") or 0.0),
                    "y1": float(bbox.get("y1") or 0.0),
                    "x2": float(bbox.get("x2") or 0.0),
                    "y2": float(bbox.get("y2") or 0.0),
                },
                "assignment_status": str(inst.get("assignment_status") or "UNREVIEWED").upper(),
            }
        )

    return {
        "image_id": str(image.get("image_id") or "").strip() or None,
        "img_name": (str(image.get("original_filename") or "").strip() or None),
        "image_role": str(image.get("image_role") or "DAILY").upper(),
        "captured_at": image.get("captured_at"),
        "width": int(image.get("width") or 0),
        "height": int(image.get("height") or 0),
        "instances": instances,
    }


def _day_range_ts(day: date_type) -> tuple[int, int]:
    tz = business_tz()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return int(start_utc.timestamp()), int(end_utc.timestamp())


def _month_start(month: str) -> date_type:
    try:
        return datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid month: {month}. Expected YYYY-MM") from exc


def _meta_business_day(meta: dict) -> Optional[date_type]:
    img = meta.get("image") or {}
    ts = img.get("captured_at_ts") or img.get("uploaded_at_ts")
    try:
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(business_tz()).date()
    except Exception:
        return None


def _build_item_from_db(image_id: str, agg: dict, meta: Optional[dict]) -> GalleryImageItem:
    pet_ids = sorted([str(x).strip() for x in (agg.get("pet_hits") or set()) if str(x).strip()])
    if meta is not None:
        item = _build_item(meta)
        item.instance_count = int(agg.get("instance_count") or item.instance_count)
        if pet_ids:
            item.pet_ids = pet_ids
        return item

    cap_ts = agg.get("captured_at_ts")
    captured_at = agg.get("captured_at")
    uploaded_at = agg.get("uploaded_at") or datetime.now(timezone.utc)
    if cap_ts is not None:
        try:
            cap_dt = datetime.fromtimestamp(int(cap_ts), tz=timezone.utc)
            captured_at = cap_dt
        except Exception:
            pass

    return GalleryImageItem(
        image_id=image_id,
        image_role=str(agg.get("image_role") or "DAILY"),
        trainer_id=agg.get("trainer_id"),
        captured_at=captured_at,
        uploaded_at=uploaded_at,
        ingest_status=agg.get("ingest_status"),
        pipeline_stage=agg.get("pipeline_stage"),
        width=int(agg.get("width") or 0),
        height=int(agg.get("height") or 0),
        raw_url=f"{settings.api_prefix}/images/{image_id}?variant=raw",
        thumb_url=f"{settings.api_prefix}/images/{image_id}?variant=thumb",
        img_name=(
            (str((meta or {}).get("image", {}).get("original_filename") or "").strip() or None)
            if meta is not None
            else (str(agg.get("original_filename") or "").strip() or None)
        ),
        instance_count=int(agg.get("instance_count") or 0),
        pet_ids=pet_ids,
    )


def _pick_meta_ts(meta: dict) -> Optional[int]:
    img = meta.get("image") or {}
    ts = img.get("captured_at_ts") or img.get("uploaded_at_ts")
    try:
        return int(ts) if ts is not None else None
    except Exception:
        return None


def _entry_from_meta(meta: dict) -> Optional[dict]:
    img = meta.get("image") or {}
    image_id = str(img.get("image_id") or "").strip()
    if not image_id:
        return None
    image_role = str(img.get("image_role") or "DAILY").upper()
    if image_role not in ("DAILY", "SEED"):
        image_role = "DAILY"
    instances = list(meta.get("instances") or [])
    pet_hits = {
        str(i.get("pet_id") or "").strip()
        for i in instances
        if str(i.get("assignment_status") or "").upper() == "ACCEPTED" and str(i.get("pet_id") or "").strip()
    }
    return {
        "image_id": image_id,
        "image_role": image_role,
        "trainer_id": img.get("trainer_id"),
        "captured_at_ts": _pick_meta_ts(meta),
        "captured_at": img.get("captured_at"),
        "uploaded_at": datetime.fromisoformat(str(img.get("uploaded_at")).replace("Z", "+00:00")) if img.get("uploaded_at") else None,
        "ingest_status": str(img.get("ingest_status") or "").upper() or None,
        "pipeline_stage": str(img.get("pipeline_stage") or "").strip() or None,
        "width": int(img.get("width") or 0),
        "height": int(img.get("height") or 0),
        "original_filename": str(img.get("original_filename") or "").strip() or None,
        "instance_count": int(img.get("instance_count") or len(instances) or 0),
        "has_unclassified": _is_unclassified(meta),
        "pet_hits": set(pet_hits),
    }


def _entry_from_image_record(record: ImageRecord) -> dict:
    base_dt = record.captured_at or record.uploaded_at
    captured_at_ts = int(base_dt.timestamp()) if base_dt is not None else None
    return {
        "image_id": record.image_id,
        "image_role": str(record.image_role or "DAILY").upper(),
        "trainer_id": record.trainer_id,
        "captured_at_ts": captured_at_ts,
        "captured_at": record.captured_at,
        "uploaded_at": record.uploaded_at,
        "ingest_status": str(record.ingest_status or "").upper() or None,
        "pipeline_stage": str(record.pipeline_stage or "").strip() or None,
        "width": int(record.width or 0),
        "height": int(record.height or 0),
        "original_filename": record.original_filename,
        "instance_count": int(record.source_detection_count or 0),
        "has_unclassified": True,
        "pet_hits": set(),
    }


@router.get("/images/calendar", response_model=ImagesCalendarResponse)
async def images_calendar(
    month: str = Query(..., description="Business timezone month filter (YYYY-MM)"),
):
    month_start = _month_start(month)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1, day=1)

    counts: Dict[str, int] = {}
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if meta_dir.exists():
        for meta_path in meta_dir.glob("img_*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            img = meta.get("image") or {}
            role = str(img.get("image_role") or "DAILY").upper()
            if role != "DAILY":
                continue
            biz_day = _meta_business_day(meta)
            if biz_day is None or biz_day < month_start or biz_day >= next_month:
                continue
            key = biz_day.isoformat()
            counts[key] = counts.get(key, 0) + 1

    items = [CalendarDayCountItem(date=day, count=count) for day, count in sorted(counts.items())]
    return ImagesCalendarResponse(month=month_start.strftime("%Y-%m"), days=items)


@router.get("/images", response_model=ImagesListResponse)
async def list_images(
    request: Request,
    date: Optional[str] = Query(default=None, description="Business timezone date filter (YYYY-MM-DD)"),
    tab: Literal["ALL", "UNCLASSIFIED", "PET"] = Query(default="ALL"),
    pet_id: Optional[str] = Query(default=None),
    include_seed: bool = Query(default=False, description="Include seed(exemplar) images in results."),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
):
    if date:
        try:
            day_obj = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid date: {date}. Expected YYYY-MM-DD") from e
    else:
        day_obj = None
    if tab == "PET" and not pet_id:
        raise HTTPException(status_code=400, detail="pet_id is required when tab=PET")

    store = _get_store(request)
    from_ts: Optional[int] = None
    to_ts: Optional[int] = None
    if day_obj is not None:
        from_ts, to_ts = _day_range_ts(day_obj)

    qf = build_filter(captured_from_ts=from_ts, captured_to_ts=to_ts)
    points = await run_in_threadpool(store.scroll_points, qf, 1000, False)

    by_image: Dict[str, dict] = {}
    for p in points:
        payload = p.payload or {}
        image_id = str(payload.get("image_id") or "")
        if not image_id:
            continue

        image_role = str(payload.get("image_role") or "DAILY").upper()
        if image_role not in ("DAILY", "SEED"):
            image_role = "DAILY"
        if (not include_seed) and image_role == "SEED":
            continue

        entry = by_image.setdefault(
            image_id,
            {
                "image_id": image_id,
                "image_role": image_role,
                "trainer_id": payload.get("trainer_id"),
                "captured_at_ts": payload.get("captured_at_ts"),
                "instance_count": 0,
                "has_unclassified": False,
                "pet_hits": set(),
            },
        )
        entry["instance_count"] += 1
        if entry.get("captured_at_ts") is None and payload.get("captured_at_ts") is not None:
            entry["captured_at_ts"] = payload.get("captured_at_ts")

        status = str(payload.get("assignment_status") or "").upper()
        p_pet_id = str(payload.get("pet_id") or "").strip()
        if status == "ACCEPTED" and p_pet_id:
            entry["pet_hits"].add(p_pet_id)
        else:
            entry["has_unclassified"] = True

    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if meta_dir.exists():
        for meta_path in meta_dir.glob("img_*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entry = _entry_from_meta(meta)
            if entry is None:
                continue
            if (not include_seed) and entry["image_role"] == "SEED":
                continue
            if from_ts is not None and to_ts is not None:
                ts = entry.get("captured_at_ts")
                if ts is None or int(ts) < from_ts or int(ts) >= to_ts:
                    continue
            existing = by_image.get(entry["image_id"])
            if existing is None:
                by_image[entry["image_id"]] = entry
            else:
                if existing.get("captured_at_ts") is None and entry.get("captured_at_ts") is not None:
                    existing["captured_at_ts"] = entry.get("captured_at_ts")
                if not existing.get("trainer_id") and entry.get("trainer_id"):
                    existing["trainer_id"] = entry.get("trainer_id")
                existing["instance_count"] = max(int(existing.get("instance_count") or 0), int(entry.get("instance_count") or 0))
                existing["has_unclassified"] = bool(existing.get("has_unclassified")) or bool(entry.get("has_unclassified"))
                existing["pet_hits"].update(entry.get("pet_hits") or set())

    db = getattr(request.app.state, "db", None)
    if db is not None:
        with db.session_scope() as session:
            stmt = select(ImageRecord).where(ImageRecord.ingest_status == "READY")
            if not include_seed:
                stmt = stmt.where(ImageRecord.image_role != "SEED")
            if day_obj is not None:
                stmt = stmt.where(ImageRecord.business_date == day_obj)
            records = list(session.execute(stmt).scalars())
            for record in records:
                entry = _entry_from_image_record(record)
                existing = by_image.get(record.image_id)
                if existing is None:
                    by_image[record.image_id] = entry
                    continue
                if existing.get("captured_at_ts") is None and entry.get("captured_at_ts") is not None:
                    existing["captured_at_ts"] = entry.get("captured_at_ts")
                if not existing.get("captured_at") and entry.get("captured_at") is not None:
                    existing["captured_at"] = entry.get("captured_at")
                if not existing.get("uploaded_at") and entry.get("uploaded_at") is not None:
                    existing["uploaded_at"] = entry.get("uploaded_at")
                if not existing.get("ingest_status") and entry.get("ingest_status"):
                    existing["ingest_status"] = entry.get("ingest_status")
                if not existing.get("pipeline_stage") and entry.get("pipeline_stage"):
                    existing["pipeline_stage"] = entry.get("pipeline_stage")
                if not existing.get("trainer_id") and entry.get("trainer_id"):
                    existing["trainer_id"] = entry.get("trainer_id")
                existing["width"] = max(int(existing.get("width") or 0), int(entry.get("width") or 0))
                existing["height"] = max(int(existing.get("height") or 0), int(entry.get("height") or 0))
                if not existing.get("original_filename") and entry.get("original_filename"):
                    existing["original_filename"] = entry.get("original_filename")
                existing["instance_count"] = max(int(existing.get("instance_count") or 0), int(entry.get("instance_count") or 0))
                existing["has_unclassified"] = bool(existing.get("has_unclassified")) or bool(entry.get("has_unclassified"))
                existing["pet_hits"].update(entry.get("pet_hits") or set())

    filtered: List[dict] = []
    for _img_id, entry in by_image.items():
        if tab == "ALL":
            filtered.append(entry)
            continue
        if tab == "UNCLASSIFIED":
            if entry["has_unclassified"]:
                filtered.append(entry)
            continue
        if pet_id in entry["pet_hits"]:
            filtered.append(entry)

    filtered.sort(key=lambda x: int(x.get("captured_at_ts") or 0), reverse=True)
    sliced = filtered[offset : offset + limit]

    items: List[GalleryImageItem] = []
    for e in sliced:
        meta = _read_meta_safe(str(e["image_id"]))
        items.append(_build_item_from_db(str(e["image_id"]), e, meta))

    return ImagesListResponse(count=len(filtered), items=items)


@router.get("/daily/{day}/zip")
async def download_daily_zip(
    day: date_type,
    root_folder_name: Optional[str] = Query(default=None, description="Archive root folder name"),
    mode: Literal["all", "accepted_only"] = Query(default="all", description="Download all daily images or only images with at least one ACCEPTED instance"),
):
    root_name = _safe_archive_name(root_folder_name, day.isoformat())
    zip_path = _zip_temp_path(f"daily_{day.isoformat()}")
    written = 0
    used_paths = set()
    pet_name_map = read_pet_name_map()

    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        raise HTTPException(status_code=404, detail="No daily image files available for zip export")

    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for meta_path in meta_dir.glob("img_*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            img = meta.get("image") or {}
            role = str(img.get("image_role") or "DAILY").upper()
            if role == "SEED":
                continue
            if mode == "accepted_only":
                has_accepted = any(str(inst.get("assignment_status") or "").upper() == "ACCEPTED" for inst in (meta.get("instances") or []))
                if not has_accepted:
                    continue
            ts = img.get("captured_at_ts") or img.get("uploaded_at_ts")
            try:
                biz_day = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(business_tz()).date()
            except Exception:
                continue
            if biz_day != day:
                continue
            src = Path(str(img.get("raw_path") or ""))
            if not src.exists():
                continue
            base_name = str(img.get("original_filename") or "").strip() or src.name
            file_name = _safe_archive_name(base_name, src.name)
            arcname = f"{root_name}/{file_name}"
            image_id = str(img.get("image_id") or "")
            if arcname in used_paths:
                file_name = _safe_archive_name(f"{image_id}_{base_name}", f"{image_id}_{src.name}")
                arcname = f"{root_name}/{file_name}"
            used_paths.add(arcname)
            zf.write(src, arcname)

            anno_name = _annotation_name_for(file_name)
            anno_arcname = f"{root_name}/{anno_name}"
            if anno_arcname in used_paths:
                anno_name = _annotation_name_for(f"{image_id}_{file_name}")
                anno_arcname = f"{root_name}/{anno_name}"
            used_paths.add(anno_arcname)
            anno_payload = _daily_annotation_payload(meta, pet_name_map)
            zf.writestr(anno_arcname, json.dumps(anno_payload, ensure_ascii=False, indent=2))
            written += 1

    if written == 0:
        raise HTTPException(status_code=404, detail="No daily image files available for zip export")

    return FileResponse(
        path=zip_path,
        media_type='application/zip',
        filename=f"{root_name}.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )


@router.get("/images/{image_id}")
def get_image(
    request: Request,
    image_id: str,
    variant: str = Query(default="raw", description="raw|thumb"),
):
    meta = _read_meta_safe(image_id)
    path = None
    if meta is not None:
        img = meta.get("image") or {}
        path = img.get("thumb_path") if variant == "thumb" else img.get("raw_path")
    if not path:
        db = _get_db(request)
        with db.session_scope() as session:
            repo = ReIdRepository(session)
            image = repo.get_image(image_id)
            if image is None:
                raise HTTPException(status_code=404, detail="Image file not found")
            path = image.thumb_path if variant == "thumb" else image.raw_path
    if not path:
        raise HTTPException(status_code=404, detail="Image file not found")
    p = Path(str(path))
    if not p.exists():
        raise HTTPException(status_code=404, detail="Image file missing on disk")
    media_type, _ = mimetypes.guess_type(str(p))
    return FileResponse(p, media_type=media_type or "application/octet-stream")


@router.get("/images/{image_id}/meta", response_model=ImageMetaResponse)
def get_image_meta(request: Request, image_id: str):
    meta = _read_meta_safe(image_id)
    if meta is not None:
        item = _build_item(meta)
        instances = []
        for x in meta.get("instances") or []:
            bb = x.get("bbox") or {}
            instances.append(
                InstanceOut(
                    instance_id=str(x.get("instance_id")),
                    class_id=int(x.get("class_id") or 0),
                    species=str(x.get("species") or "UNKNOWN"),
                    confidence=float(x.get("confidence") or 0.0),
                    bbox=BBox(
                        x1=float(bb.get("x1") or 0.0),
                        y1=float(bb.get("y1") or 0.0),
                        x2=float(bb.get("x2") or 0.0),
                        y2=float(bb.get("y2") or 0.0),
                    ),
                    pet_id=(str(x.get("pet_id")) if x.get("pet_id") is not None else None),
                )
            )
        return ImageMetaResponse(image=item, instances=instances)

    db = _get_db(request)
    with db.session_scope() as session:
        repo = ReIdRepository(session)
        image = repo.get_image(image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="Image meta not found")

    item = _build_item_from_db(
        image_id,
        {
            "image_id": image.image_id,
            "image_role": image.image_role,
            "trainer_id": image.trainer_id,
            "captured_at_ts": int((image.captured_at or image.uploaded_at).timestamp()) if (image.captured_at or image.uploaded_at) else None,
            "captured_at": image.captured_at,
            "uploaded_at": image.uploaded_at,
            "ingest_status": image.ingest_status,
            "pipeline_stage": image.pipeline_stage,
            "width": image.width,
            "height": image.height,
            "original_filename": image.original_filename,
            "instance_count": int(image.source_detection_count or 0),
            "pet_hits": set(),
        },
        None,
    )
    return ImageMetaResponse(image=item, instances=[])


@router.delete("/images/{image_id}", response_model=ImageDeleteResponse)
async def delete_daily_image(
    request: Request,
    image_id: str,
    updated_by: Optional[str] = Query(default=None),
):
    meta = _read_meta(image_id)
    image = meta.get("image") or {}
    image_role = str(image.get("image_role") or "DAILY").upper()
    if image_role != "DAILY":
        raise HTTPException(status_code=400, detail="Only DAILY images can be deleted here")

    store = _get_store(request)
    instance_ids = _instance_ids_for_image(meta)
    if not instance_ids:
        qf = qm.Filter(must=[qm.FieldCondition(key="image_id", match=qm.MatchValue(value=image_id))])
        points = await run_in_threadpool(store.scroll_points, qf, 1000, False)
        instance_ids = [store.external_instance_id(str(p.point_id)) for p in points if str((p.payload or {}).get("image_id") or "").strip() == image_id]

    if instance_ids:
        await run_in_threadpool(store.delete_points, instance_ids)

    deleted_files = await run_in_threadpool(_delete_daily_image_assets, image_id, meta)
    return ImageDeleteResponse(image_id=image_id, deleted_points=len(instance_ids), deleted_files=deleted_files)
