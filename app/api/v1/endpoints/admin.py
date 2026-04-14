"""관리자 대시보드용 보조 엔드포인트 모듈."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from app.utils.timezone import business_tz

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.admin import AdminImageLabelItem, AdminImageLabelRequest, AdminImageLabelResponse
from app.vector_db.qdrant_store import PointRecord, QdrantStore, build_filter

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


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
