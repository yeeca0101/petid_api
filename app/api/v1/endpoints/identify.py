"""단일 업로드 이미지에 대해 exemplar 기반 pet 후보를 반환하는 엔드포인트 모듈."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from app.utils.timezone import business_tz

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from qdrant_client.http import models as qm
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.ingest import ingest_image_sync
from app.api.v1.endpoints.pets import _read_pet_name_map
from app.schemas.identify import IdentifyCandidate, IdentifyResponse
from app.vector_db.qdrant_store import QdrantStore

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _resolve_captured_at(captured_at: Optional[str]) -> tuple[datetime, str]:
    if captured_at:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid captured_at: {captured_at}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=business_tz())
        return parsed, parsed.isoformat()

    now = datetime.now(timezone.utc)
    return now, now.isoformat()


def _exemplar_filter(species: str) -> qm.Filter:
    must: List[qm.FieldCondition] = [
        qm.FieldCondition(key="is_seed", match=qm.MatchValue(value=True)),
        qm.FieldCondition(key="seed_active", match=qm.MatchValue(value=True)),
    ]
    if species in {"DOG", "CAT"}:
        must.append(qm.FieldCondition(key="species", match=qm.MatchValue(value=species)))
    return qm.Filter(must=must)


@router.post("/identify", response_model=IdentifyResponse)
async def identify(
    request: Request,
    file: UploadFile = File(...),
    captured_at: Optional[str] = Form(default=None, description="ISO8601 timestamp"),
    top_k: int = Form(default=1, ge=1, le=100),
):
    store = _get_store(request)
    _base_dt, resolved_captured_at = _resolve_captured_at(captured_at)

    ingest_resp = await ingest_image_sync(
        request=request,
        file=file,
        daycare_id=None,
        trainer_id=None,
        captured_at=resolved_captured_at,
        image_role="DAILY",
        pet_name=None,
        include_embedding=False,
    )

    instances = list(ingest_resp.instances or [])
    if not instances:
        raise HTTPException(status_code=400, detail="No detected instances in uploaded image")

    target = max(instances, key=lambda x: float(x.confidence))
    points = await run_in_threadpool(store.retrieve_points, [target.instance_id], True)
    point = points.get(target.instance_id)
    if point is None or point.vector is None or len(point.vector) == 0:
        raise HTTPException(status_code=404, detail="No query instance vector found in vector DB")

    hits = await run_in_threadpool(
        store.search,
        point.vector,
        max(top_k * 10, top_k),
        _exemplar_filter(species=target.species),
    )

    pet_name_map = _read_pet_name_map()
    candidates: List[IdentifyCandidate] = []
    seen_pet_ids = set()

    for hit in hits:
        payload = hit.payload or {}
        pet_id = str(payload.get("seed_pet_id") or "").strip()
        if not pet_id or pet_id in seen_pet_ids:
            continue
        seen_pet_ids.add(pet_id)
        pet_name = pet_name_map.get(pet_id) or (str(payload.get("pet_name") or "").strip() or None)
        candidates.append(
            IdentifyCandidate(
                pet_id=pet_id,
                pet_name=pet_name,
                score=float(hit.score),
            )
        )
        if len(candidates) >= top_k:
            break

    return IdentifyResponse(
        image_id=ingest_resp.image.image_id,
        instance_id=target.instance_id,
        species=target.species,
        bbox=target.bbox,
        candidates=candidates,
    )
