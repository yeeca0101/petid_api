"""등록/라벨된 반려동물 목록을 조회하는 엔드포인트 모듈."""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.schemas.pets import PetListItem, PetsListResponse
from app.utils.pet_registry import read_pet_name_map
from app.vector_db.qdrant_store import QdrantStore, build_filter

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _read_pet_name_map() -> Dict[str, str]:
    return read_pet_name_map()


@router.get("/pets", response_model=PetsListResponse)
async def list_pets(request: Request):
    """List global pet IDs used across all stored exemplars/labels, with optional display names."""
    store = _get_store(request)
    pet_name_map = _read_pet_name_map()
    agg: Dict[str, dict] = {}

    qf = build_filter()
    points = await run_in_threadpool(store.scroll_points, qf, 1000, False)
    for p in points:
        payload = p.payload or {}
        image_id = str(payload.get("image_id") or "")

        pet_ids = set()
        pet_id_label = str(payload.get("pet_id") or "").strip()
        pet_id_seed = str(payload.get("seed_pet_id") or "").strip()
        if pet_id_label:
            pet_ids.add(pet_id_label)
        if pet_id_seed:
            pet_ids.add(pet_id_seed)

        payload_pet_name = str(payload.get("pet_name") or "").strip() or None
        for pet_id in pet_ids:
            item = agg.setdefault(
                pet_id,
                {"pet_id": pet_id, "pet_name": pet_name_map.get(pet_id), "images": set(), "instances": 0},
            )
            if item.get("pet_name") in (None, "") and payload_pet_name:
                item["pet_name"] = payload_pet_name
            if image_id:
                item["images"].add(image_id)
            item["instances"] += 1

    items = [
        PetListItem(
            pet_id=v["pet_id"],
            pet_name=v.get("pet_name"),
            image_count=len(v["images"]),
            instance_count=int(v["instances"]),
        )
        for _k, v in sorted(agg.items(), key=lambda kv: kv[0])
    ]
    return PetsListResponse(count=len(items), items=items)
