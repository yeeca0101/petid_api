"""벡터 유사도 검색으로 후보 이미지를 조회하는 엔드포인트 모듈."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from app.utils.timezone import business_tz

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.ingest import BBox
from app.schemas.search import (
    BestMatch,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from app.vector_db.qdrant_store import QdrantStore, build_filter

router = APIRouter()


def _get_store(request: Request) -> QdrantStore:
    store = getattr(request.app.state, "vector_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Vector DB not ready")
    return store


def _to_ts(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=business_tz())
    return int(dt.timestamp())


def _rrf_fusion_image(result_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    """Reciprocal Rank Fusion over image_ids.

    When multiple exemplar vectors are used, we want to reward images that
    appear high in multiple per-query rankings.
    """
    fused: Dict[str, float] = {}
    for lst in result_lists:
        for rank, image_id in enumerate(lst, start=1):
            fused[image_id] = fused.get(image_id, 0.0) + 1.0 / (k + rank)
    return fused


@router.post("/search", response_model=SearchResponse)
async def search(request: Request, body: SearchRequest):
    store = _get_store(request)

    if not body.query.instance_ids:
        raise HTTPException(status_code=400, detail="query.instance_ids is required")

    query_ids = list(dict.fromkeys(body.query.instance_ids))
    try:
        query_ids_norm = set(store.normalize_instance_ids(query_ids))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    query_ids_all = set(query_ids) | query_ids_norm

    vecs = await run_in_threadpool(store.retrieve_vectors, query_ids)
    if not vecs:
        raise HTTPException(status_code=404, detail="No query instance vectors found in vector DB")

    # Filters
    species = body.filters.species if body.filters else None
    f = build_filter(
        species=species,
        captured_from_ts=_to_ts(body.filters.captured_from) if body.filters else None,
        captured_to_ts=_to_ts(body.filters.captured_to) if body.filters else None,
    )

    # Search per query vector (instance-level), then aggregate to image-level per query
    per_query_image_ranked: List[List[str]] = []

    # We keep both: a ranking score (rrf or max) and the best cosine similarity for UI.
    best_sim: Dict[str, float] = {}
    best_match: Dict[str, Tuple[str, float, Optional[BBox]]] = {}

    for _qid, qv in vecs.items():
        hits = await run_in_threadpool(store.search, qv, body.per_query_limit, f)

        # image_id -> (best_pid, best_score, bbox)
        best_for_image: Dict[str, Tuple[str, float, Optional[BBox]]] = {}

        for h in hits:
            if h.point_id in query_ids_all:
                continue
            payload = h.payload or {}
            image_id = str(payload.get("image_id", ""))
            if not image_id:
                continue
            bb = payload.get("bbox")
            bbox_obj = None
            if isinstance(bb, dict) and all(k in bb for k in ("x1", "y1", "x2", "y2")):
                bbox_obj = BBox(**bb)

            prev = best_for_image.get(image_id)
            if prev is None or h.score > prev[1]:
                best_for_image[image_id] = (h.point_id, float(h.score), bbox_obj)

            if h.score > best_sim.get(image_id, float("-inf")):
                best_sim[image_id] = float(h.score)
                best_match[image_id] = (h.point_id, float(h.score), bbox_obj)

        ranked_images = sorted(best_for_image.items(), key=lambda x: x[1][1], reverse=True)
        per_query_image_ranked.append([img_id for img_id, _ in ranked_images])

    merge = body.query.merge.upper()
    if merge == "MAX":
        ranking_score: Dict[str, float] = dict(best_sim)
    else:
        ranking_score = _rrf_fusion_image(per_query_image_ranked)

    # Build final sorted list (ranking_score), but return also best cosine via best_match.score
    ordered = sorted(
        ranking_score.items(),
        key=lambda x: (x[1], best_sim.get(x[0], 0.0)),
        reverse=True,
    )
    ordered = ordered[: body.top_k_images]

    results: List[SearchResultItem] = []
    for image_id, score in ordered:
        pid, sim, bbox_obj = best_match.get(image_id, ("", 0.0, None))
        pid = store.external_instance_id(pid) if pid else pid
        results.append(
            SearchResultItem(
                image_id=image_id,
                score=float(score),
                best_match=BestMatch(instance_id=pid, bbox=bbox_obj, score=float(sim)),
            )
        )

    return SearchResponse(
        query_debug={
            "used_vectors": len(vecs),
            "merge": merge,
            "per_query_limit": body.per_query_limit,
            "top_k_images": body.top_k_images,
        },
        results=results,
    )
