"""인스턴스에 pet 라벨 정보를 기록하는 엔드포인트 모듈."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.labels import LabelRequest, LabelResponse, LabelResponseItem
from app.vector_db.qdrant_store import QdrantStore

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _build_label_payload(a, labeled_by: Optional[str], now_ts: int) -> tuple[dict, str]:
    action = a.action.upper()
    base = {
        "label_source": a.source,
        "label_confidence": float(a.confidence),
        "labeled_by": labeled_by,
        "labeled_at_ts": now_ts,
    }
    if action == "ACCEPT":
        if not a.pet_id:
            raise HTTPException(status_code=400, detail=f"pet_id is required for action=ACCEPT ({a.instance_id})")
        base["pet_id"] = a.pet_id
        base["assignment_status"] = "ACCEPTED"
        return base, "ACCEPTED"
    if action == "REJECT":
        base["pet_id"] = None
        base["assignment_status"] = "REJECTED"
        return base, "REJECTED"
    base["pet_id"] = None
    base["assignment_status"] = "UNREVIEWED"
    return base, "UNREVIEWED"


def _sync_meta_sidecars(assignments: Dict[str, dict]) -> None:
    """Mirror assignment fields into local image meta JSON sidecars."""
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        return

    for p in meta_dir.glob("img_*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        changed = False
        instances = meta.get("instances") or []
        for inst in instances:
            iid = str(inst.get("instance_id") or "")
            if iid not in assignments:
                continue
            payload = assignments[iid]
            inst["pet_id"] = payload.get("pet_id")
            inst["assignment_status"] = payload.get("assignment_status")
            inst["label_source"] = payload.get("label_source")
            inst["label_confidence"] = payload.get("label_confidence")
            inst["labeled_at_ts"] = payload.get("labeled_at_ts")
            inst["labeled_by"] = payload.get("labeled_by")
            changed = True

        if changed:
            p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@router.post("/labels", response_model=LabelResponse)
async def set_labels(request: Request, body: LabelRequest):
    """Assign (instance_id -> pet_id) labels.

    For this PoC, labels are stored in Qdrant payload.
    """

    store = _get_store(request)
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    meta_sync_payloads: Dict[str, dict] = {}
    items = []
    for a in body.assignments:
        payload, assignment_status = _build_label_payload(a, body.labeled_by, now_ts)
        try:
            await run_in_threadpool(store.set_payload, [a.instance_id], payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        meta_sync_payloads[a.instance_id] = payload
        items.append(
            LabelResponseItem(
                instance_id=a.instance_id,
                pet_id=payload.get("pet_id"),
                assignment_status=assignment_status,
                updated=True,
            )
        )

    await run_in_threadpool(_sync_meta_sidecars, meta_sync_payloads)

    return LabelResponse(labeled_at=now, items=items)
