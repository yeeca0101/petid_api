"""일자별 미분류 인스턴스를 반자동 분류하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import re
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple
from app.utils.timezone import business_tz

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.pets import _read_pet_name_map
from app.core.config import settings
from app.schemas.classification import (
    AutoClassifyItem,
    AutoClassifyRequest,
    AutoClassifyResponse,
    AutoClassifySummary,
    BucketQualityMetrics,
    FinalizeBucketImageItem,
    FinalizeBucketItem,
    FinalizeBucketsRequest,
    FinalizeBucketsResponse,
    GetBucketsResponse,
    SimilarSearchItem,
    SimilarSearchRequest,
    SimilarSearchResponse,
)
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


def _payload_instance_id(store: QdrantStore, p: PointRecord) -> str:
    pid = p.payload.get("instance_id")
    if isinstance(pid, str) and pid:
        return pid
    return store.external_instance_id(p.point_id)


def _is_target(payload: dict) -> bool:
    if bool(payload.get("is_seed", False)):
        return False
    if str(payload.get("image_role") or "").upper() == "SEED":
        return False
    pet_id = payload.get("pet_id")
    status = str(payload.get("assignment_status") or "").upper()
    if pet_id:
        return False
    return status != "ACCEPTED"


def _is_exemplar(payload: dict) -> bool:
    if not bool(payload.get("is_seed", False)):
        return False
    seed_pet_id = payload.get("seed_pet_id")
    if not seed_pet_id:
        return False
    return bool(payload.get("seed_active", True))


def _meta_day_business(meta: dict) -> str:
    img = meta.get("image") or {}
    ts = img.get("captured_at_ts") or img.get("uploaded_at_ts")
    try:
        tz = business_tz()
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz).date().isoformat()
    except Exception:
        return ""


def _is_unclassified(meta: dict) -> bool:
    instances = meta.get("instances") or []
    if not instances:
        return True
    for i in instances:
        if (i.get("assignment_status") == "ACCEPTED") and i.get("pet_id"):
            continue
        return True
    return False


def _matches_tab(meta: dict, tab: Literal["ALL", "UNCLASSIFIED", "PET"], pet_id: Optional[str]) -> bool:
    if tab == "ALL":
        return True
    if tab == "UNCLASSIFIED":
        return _is_unclassified(meta)
    instances = meta.get("instances") or []
    return any((i.get("assignment_status") == "ACCEPTED") and (i.get("pet_id") == pet_id) for i in instances)


def _is_seed_image(meta: dict) -> bool:
    img = meta.get("image") or {}
    return str(img.get("image_role") or "DAILY").upper() == "SEED"


def _load_day_metas(
    day: date,
    tab: Literal["ALL", "UNCLASSIFIED", "PET"] = "ALL",
    pet_id: Optional[str] = None,
    include_seed: bool = False,
) -> List[dict]:
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        return []
    day_str = day.isoformat()
    metas: List[dict] = []
    for p in meta_dir.glob("img_*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            img = meta.get("image") or {}
            if _meta_day_business(meta) != day_str:
                continue
            if (not include_seed) and _is_seed_image(meta):
                continue
            if not _matches_tab(meta, tab, pet_id):
                continue
            metas.append(meta)
        except Exception:
            continue
    return metas


def _rrf_fusion_image(result_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    fused: Dict[str, float] = {}
    for lst in result_lists:
        for rank, image_id in enumerate(lst, start=1):
            fused[image_id] = fused.get(image_id, 0.0) + 1.0 / (k + rank)
    return fused


def _sync_meta_sidecars(assignments: Dict[str, dict]) -> None:
    meta_dir = Path(settings.reid_storage_dir) / "meta"
    if not meta_dir.exists():
        return
    for p in meta_dir.glob("img_*.json"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        changed = False
        for inst in meta.get("instances") or []:
            iid = str(inst.get("instance_id") or "")
            payload = assignments.get(iid)
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
            p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@router.post("/classify/auto", response_model=AutoClassifyResponse)
async def auto_classify(request: Request, body: AutoClassifyRequest):
    """Assign candidate pet IDs for one daycare/date using similarity search."""
    if body.candidate_threshold > body.auto_accept_threshold:
        raise HTTPException(status_code=400, detail="candidate_threshold must be <= auto_accept_threshold")

    store = _get_store(request)
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    start_ts, end_ts = _day_range_ts(body.date)
    day_filter = build_filter(
        species=body.species,
        captured_from_ts=start_ts,
        captured_to_ts=end_ts,
    )

    points = await run_in_threadpool(store.scroll_points, day_filter, 1000, True)
    targets = [p for p in points if _is_target(p.payload) and p.vector is not None and len(p.vector) > 0]

    accepted = 0
    unreviewed_candidate = 0
    unreviewed_no_candidate = 0
    unchanged = 0
    items: list[AutoClassifyItem] = []
    meta_sync_payloads: Dict[str, dict] = {}

    for p in targets:
        instance_id = _payload_instance_id(store, p)
        image_id = str(p.payload.get("image_id") or "")
        species = str(p.payload.get("species") or "UNKNOWN")

        search_filter = build_filter(species=species if species in ("DOG", "CAT") else None)
        hits = await run_in_threadpool(store.search, p.vector, body.search_limit, search_filter)

        best_pet_id: Optional[str] = None
        best_score: Optional[float] = None
        for h in hits:
            if str(h.point_id) == str(p.point_id):
                continue
            if not _is_exemplar(h.payload):
                continue
            best_pet_id = str(h.payload.get("seed_pet_id"))
            best_score = float(h.score)
            break

        new_payload: Optional[dict] = None
        assignment_status = "UNREVIEWED"
        selected_pet_id: Optional[str] = None

        if best_pet_id is None or best_score is None or best_score < body.candidate_threshold:
            unreviewed_no_candidate += 1
            assignment_status = "UNREVIEWED"
            new_payload = {
                "pet_id": None,
                "auto_pet_id": None,
                "auto_score": None,
                "assignment_status": "UNREVIEWED",
                "label_source": "AUTO",
                "label_confidence": 0.0,
                "labeled_by": body.labeled_by,
                "labeled_at_ts": now_ts,
            }
        elif best_score >= body.auto_accept_threshold:
            accepted += 1
            assignment_status = "ACCEPTED"
            selected_pet_id = best_pet_id
            new_payload = {
                "pet_id": best_pet_id,
                "auto_pet_id": best_pet_id,
                "auto_score": float(best_score),
                "assignment_status": "ACCEPTED",
                "label_source": "AUTO",
                "label_confidence": float(best_score),
                "labeled_by": body.labeled_by,
                "labeled_at_ts": now_ts,
            }
        else:
            unreviewed_candidate += 1
            assignment_status = "UNREVIEWED"
            selected_pet_id = best_pet_id
            new_payload = {
                "pet_id": None,
                "auto_pet_id": best_pet_id,
                "auto_score": float(best_score),
                "assignment_status": "UNREVIEWED",
                "label_source": "AUTO",
                "label_confidence": float(best_score),
                "labeled_by": body.labeled_by,
                "labeled_at_ts": now_ts,
            }

        updated = False
        if new_payload is not None:
            current_key = (
                p.payload.get("pet_id"),
                p.payload.get("auto_pet_id"),
                p.payload.get("auto_score"),
                p.payload.get("assignment_status"),
            )
            next_key = (
                new_payload.get("pet_id"),
                new_payload.get("auto_pet_id"),
                new_payload.get("auto_score"),
                new_payload.get("assignment_status"),
            )
            if current_key == next_key:
                unchanged += 1
            else:
                updated = True
                if not body.dry_run:
                    await run_in_threadpool(store.set_payload, [instance_id], new_payload)
                    meta_sync_payloads[instance_id] = new_payload

        items.append(
            AutoClassifyItem(
                instance_id=instance_id,
                image_id=image_id,
                species=species,
                score=best_score,
                selected_pet_id=selected_pet_id,
                assignment_status=assignment_status,
                updated=updated,
            )
        )

    if (not body.dry_run) and meta_sync_payloads:
        await run_in_threadpool(_sync_meta_sidecars, meta_sync_payloads)

    return AutoClassifyResponse(
        requested_at=now,
        date=body.date,
        dry_run=body.dry_run,
        summary=AutoClassifySummary(
            scanned_instances=len(targets),
            accepted=accepted,
            unreviewed_candidate=unreviewed_candidate,
            unreviewed_no_candidate=unreviewed_no_candidate,
            unchanged=unchanged,
        ),
        items=items,
    )


@router.post("/classify/similar", response_model=SimilarSearchResponse)
async def classify_similar(request: Request, body: SimilarSearchRequest):
    """Re-rank images by similarity within the currently visible tab scope."""
    if body.tab == "PET" and not body.pet_id:
        raise HTTPException(status_code=400, detail="pet_id is required when tab=PET")

    store = _get_store(request)
    now = datetime.now(timezone.utc)
    metas = _load_day_metas(
        day=body.date,
        tab=body.tab,
        pet_id=body.pet_id,
        include_seed=body.include_seed,
    )
    allowed_image_ids: Set[str] = set()
    image_urls: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for m in metas:
        img = m.get("image") or {}
        iid = str(img.get("image_id") or "")
        if iid:
            allowed_image_ids.add(iid)
            image_urls[iid] = (
                img.get("raw_url"),
                img.get("thumb_url"),
            )

    if not allowed_image_ids:
        return SimilarSearchResponse(
            requested_at=now,
            date=body.date,
            tab=body.tab,
            pet_id=body.pet_id,
            query_debug={"used_vectors": 0, "merge": body.merge, "allowed_images": 0},
            results=[],
        )

    query_ids = list(dict.fromkeys(body.query_instance_ids))
    try:
        query_ids_norm = set(store.normalize_instance_ids(query_ids))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    query_ids_all = set(query_ids) | query_ids_norm

    vecs = await run_in_threadpool(store.retrieve_vectors, query_ids)
    if not vecs:
        raise HTTPException(status_code=404, detail="No query instance vectors found in vector DB")

    start_ts, end_ts = _day_range_ts(body.date)
    f = build_filter(captured_from_ts=start_ts, captured_to_ts=end_ts)

    per_query_image_ranked: List[List[str]] = []
    best_sim: Dict[str, float] = {}
    best_match: Dict[str, Tuple[str, float]] = {}

    for _qid, qv in vecs.items():
        hits = await run_in_threadpool(store.search, qv, body.per_query_limit, f)
        best_for_image: Dict[str, Tuple[str, float]] = {}

        for h in hits:
            if h.point_id in query_ids_all:
                continue
            payload = h.payload or {}
            image_id = str(payload.get("image_id") or "")
            if not image_id or image_id not in allowed_image_ids:
                continue

            prev = best_for_image.get(image_id)
            if prev is None or h.score > prev[1]:
                best_for_image[image_id] = (h.point_id, float(h.score))
            if h.score > best_sim.get(image_id, float("-inf")):
                best_sim[image_id] = float(h.score)
                best_match[image_id] = (h.point_id, float(h.score))

        ranked_images = sorted(best_for_image.items(), key=lambda x: x[1][1], reverse=True)
        per_query_image_ranked.append([img_id for img_id, _ in ranked_images])

    merge = body.merge.upper()
    if merge == "MAX":
        ranking_score: Dict[str, float] = dict(best_sim)
    else:
        ranking_score = _rrf_fusion_image(per_query_image_ranked)

    ordered = sorted(
        ranking_score.items(),
        key=lambda x: (x[1], best_sim.get(x[0], 0.0)),
        reverse=True,
    )[: body.top_k_images]

    results: List[SimilarSearchItem] = []
    for image_id, score in ordered:
        pid, sim = best_match.get(image_id, ("", 0.0))
        pid = store.external_instance_id(pid) if pid else None
        results.append(
            SimilarSearchItem(
                image_id=image_id,
                score=float(score),
                best_match_instance_id=pid,
                best_match_score=float(sim),
                raw_url=image_urls.get(image_id, (None, None))[0],
                thumb_url=image_urls.get(image_id, (None, None))[1],
            )
        )

    return SimilarSearchResponse(
        requested_at=now,
        date=body.date,
        tab=body.tab,
        pet_id=body.pet_id,
        query_debug={
            "used_vectors": len(vecs),
            "merge": merge,
            "per_query_limit": body.per_query_limit,
            "top_k_images": body.top_k_images,
            "allowed_images": len(allowed_image_ids),
        },
        results=results,
    )


def _manifest_dir(day: date) -> Path:
    return Path(settings.reid_storage_dir) / "buckets" / day.isoformat()


def _meta_path(image_id: str) -> Path:
    return Path(settings.reid_storage_dir) / "meta" / f"{image_id}.json"


def _read_meta_safe(image_id: str) -> Optional[dict]:
    p = _meta_path(image_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_archive_name(name: Optional[str], default: str = "unknown") -> str:
    raw = (name or "").strip()
    if not raw:
        raw = default
    safe = re.sub(r'[\/:*?"<>|]+', '_', raw)
    safe = safe.replace('..', '_').strip().strip('.')
    return safe or default


def _bucket_image_from_meta(meta: dict) -> Optional[FinalizeBucketImageItem]:
    img = meta.get("image") or {}
    image_id = str(img.get("image_id") or "")
    raw_path = str(img.get("raw_path") or "")
    if not image_id or not raw_path:
        return None
    file_name = Path(raw_path).name
    return FinalizeBucketImageItem(
        image_id=image_id,
        file_name=file_name,
        original_filename=(str(img.get("original_filename")) if img.get("original_filename") is not None else None),
        raw_path=raw_path,
        raw_url=(str(img.get("raw_url")) if img.get("raw_url") is not None else None),
        captured_at=(str(img.get("captured_at")) if img.get("captured_at") is not None else None),
    )


def _bucket_item_from_raw(bucket: dict, pet_name_map: Dict[str, str]) -> FinalizeBucketItem:
    pet_id = str(bucket.get("pet_id") or "")
    raw_images = bucket.get("images") or []
    images: List[FinalizeBucketImageItem] = []
    for item in raw_images:
        if isinstance(item, dict):
            images.append(FinalizeBucketImageItem(**item))
    image_ids = bucket.get("image_ids") or [img.image_id for img in images]
    image_ids = [str(x) for x in image_ids if str(x)]
    if (not images) and image_ids:
        for image_id in image_ids:
            meta = _read_meta_safe(image_id)
            if meta is None:
                continue
            detail = _bucket_image_from_meta(meta)
            if detail is not None:
                images.append(detail)
    if not image_ids:
        image_ids = [img.image_id for img in images]
    pet_name = bucket.get("pet_name")
    if pet_name in (None, ""):
        pet_name = pet_name_map.get(pet_id)
    return FinalizeBucketItem(
        pet_id=pet_id,
        pet_name=(str(pet_name) if pet_name not in (None, "") else None),
        image_ids=image_ids,
        images=images,
        count=int(bucket.get("count") or len(image_ids)),
        instance_count=int(bucket.get("instance_count") or bucket.get("count") or len(image_ids)),
    )


def _select_manifest_path(day: date, manifest: Optional[str]) -> Path:
    dir_path = _manifest_dir(day)
    if not dir_path.exists():
        raise HTTPException(status_code=404, detail="No bucket manifests found")

    if manifest:
        p = dir_path / manifest
        if not p.exists():
            raise HTTPException(status_code=404, detail="Manifest not found")
        return p

    candidates = sorted(dir_path.glob("finalize_*.json"))
    if not candidates:
        raise HTTPException(status_code=404, detail="No bucket manifests found")
    return candidates[-1]


def _load_bucket_response(day: date, manifest: Optional[str]) -> GetBucketsResponse:
    target = _select_manifest_path(day, manifest)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read manifest: {e}") from e

    pet_name_map = _read_pet_name_map()
    buckets = [_bucket_item_from_raw(b, pet_name_map) for b in (data.get("buckets") or [])]
    qm_raw = data.get("quality_metrics") or {}
    quality_metrics = BucketQualityMetrics(
        total_day_images=int(qm_raw.get("total_day_images") or 0),
        unclassified_images=int(qm_raw.get("unclassified_images") or 0),
        unclassified_image_ratio=float(qm_raw.get("unclassified_image_ratio") or 0.0),
        total_instances=int(qm_raw.get("total_instances") or 0),
        accepted_instances=int(qm_raw.get("accepted_instances") or 0),
        accepted_auto_instances=int(qm_raw.get("accepted_auto_instances") or 0),
        unreviewed_instances=int(qm_raw.get("unreviewed_instances") or 0),
        rejected_instances=int(qm_raw.get("rejected_instances") or 0),
        auto_accept_ratio=float(qm_raw.get("auto_accept_ratio") or 0.0),
    )
    finalized_at_raw = data.get("finalized_at") or datetime.now(timezone.utc).isoformat()
    finalized_at = datetime.fromisoformat(str(finalized_at_raw).replace("Z", "+00:00"))
    return GetBucketsResponse(
        date=day,
        manifest_path=str(target),
        finalized_at=finalized_at,
        bucket_count=int(data.get("bucket_count") or len(buckets)),
        total_images=int(data.get("total_images") or 0),
        quality_metrics=quality_metrics,
        buckets=buckets,
    )


@router.post("/buckets/finalize", response_model=FinalizeBucketsResponse)
async def finalize_buckets(body: FinalizeBucketsRequest):
    """Build and persist per-pet daily image buckets from accepted assignments."""
    now = datetime.now(timezone.utc)
    metas = _load_day_metas(day=body.date, tab="ALL", pet_id=None, include_seed=False)
    allowed_pet_ids = set(body.pet_ids) if body.pet_ids else None
    pet_name_map = _read_pet_name_map()

    pet_map: Dict[str, Dict[str, FinalizeBucketImageItem]] = {}
    pet_instance_counts: Dict[str, int] = {}
    total_day_images = len(metas)
    unclassified_images = 0
    total_instances = 0
    accepted_instances = 0
    accepted_auto_instances = 0
    unreviewed_instances = 0
    rejected_instances = 0

    for m in metas:
        if _is_unclassified(m):
            unclassified_images += 1
        img = m.get("image") or {}
        image_id = str(img.get("image_id") or "")
        if not image_id:
            continue
        detail = _bucket_image_from_meta(m)
        for inst in m.get("instances") or []:
            total_instances += 1
            status = str(inst.get("assignment_status") or "UNREVIEWED").upper()
            source = str(inst.get("label_source") or "").upper()
            if status == "ACCEPTED":
                accepted_instances += 1
                if source == "AUTO":
                    accepted_auto_instances += 1
            elif status == "REJECTED":
                rejected_instances += 1
            else:
                unreviewed_instances += 1

            if inst.get("assignment_status") != "ACCEPTED":
                continue
            pet_id = str(inst.get("pet_id") or "")
            if not pet_id:
                continue
            if allowed_pet_ids is not None and pet_id not in allowed_pet_ids:
                continue
            pet_instance_counts[pet_id] = int(pet_instance_counts.get(pet_id, 0)) + 1
            pet_images = pet_map.setdefault(pet_id, {})
            if detail is not None and image_id not in pet_images:
                pet_images[image_id] = detail

    bucket_items: List[FinalizeBucketItem] = []
    unique_images: Set[str] = set()
    for pet_id in sorted(pet_map.keys()):
        images = [pet_map[pet_id][iid] for iid in sorted(pet_map[pet_id].keys())]
        ids = [img.image_id for img in images]
        unique_images.update(ids)
        bucket_items.append(
            FinalizeBucketItem(
                pet_id=pet_id,
                pet_name=pet_name_map.get(pet_id),
                image_ids=ids,
                images=images,
                count=len(ids),
                instance_count=int(pet_instance_counts.get(pet_id, 0)),
            )
        )

    quality_metrics = BucketQualityMetrics(
        total_day_images=total_day_images,
        unclassified_images=unclassified_images,
        unclassified_image_ratio=(float(unclassified_images) / float(total_day_images)) if total_day_images > 0 else 0.0,
        total_instances=total_instances,
        accepted_instances=accepted_instances,
        accepted_auto_instances=accepted_auto_instances,
        unreviewed_instances=unreviewed_instances,
        rejected_instances=rejected_instances,
        auto_accept_ratio=(float(accepted_auto_instances) / float(total_instances)) if total_instances > 0 else 0.0,
    )

    manifest_dir = _manifest_dir(body.date)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    manifest_path = manifest_dir / f"finalize_{stamp}.json"

    payload = {
        "finalized_at": now.isoformat(),
        "date": body.date.isoformat(),
        "bucket_count": len(bucket_items),
        "total_images": len(unique_images),
        "quality_metrics": quality_metrics.model_dump(mode="json"),
        "buckets": [b.model_dump(mode="json") for b in bucket_items],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return FinalizeBucketsResponse(
        finalized_at=now,
        date=body.date,
        bucket_count=len(bucket_items),
        total_images=len(unique_images),
        quality_metrics=quality_metrics,
        manifest_path=str(manifest_path),
        buckets=bucket_items,
    )


@router.get("/buckets/{day}", response_model=GetBucketsResponse)
async def get_buckets(
    day: date,
    manifest: Optional[str] = Query(default=None, description="Specific manifest filename"),
):
    """Load a persisted daily bucket manifest (latest by default)."""
    return _load_bucket_response(day=day, manifest=manifest)


@router.get("/buckets/{day}/zip")
async def download_buckets_zip(
    day: date,
    manifest: Optional[str] = Query(default=None, description="Specific manifest filename"),
    root_folder_name: Optional[str] = Query(default=None, description="Archive root folder name"),
):
    """Create and return a zip archive shaped as root_folder/pet_name/daily_images."""
    resp = _load_bucket_response(day=day, manifest=manifest)
    manifest_path = Path(resp.manifest_path)
    pet_name_map = _read_pet_name_map()
    root_name = _safe_archive_name(root_folder_name, day.isoformat())
    zip_path = manifest_path.with_suffix('.zip')

    written = 0
    used_paths: Set[str] = set()
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for bucket in resp.buckets:
            pet_folder = _safe_archive_name(bucket.pet_name or pet_name_map.get(bucket.pet_id) or bucket.pet_id, bucket.pet_id)
            images = bucket.images
            if not images and bucket.image_ids:
                fallback_images: List[FinalizeBucketImageItem] = []
                for image_id in bucket.image_ids:
                    meta = _read_meta_safe(image_id)
                    if meta is None:
                        continue
                    detail = _bucket_image_from_meta(meta)
                    if detail is not None:
                        fallback_images.append(detail)
                images = fallback_images
            for item in images:
                src = Path(item.raw_path)
                if not src.exists():
                    continue
                base_name = item.original_filename or item.file_name or src.name
                file_name = _safe_archive_name(base_name, src.name)
                arcname = f"{root_name}/{pet_folder}/{file_name}"
                if arcname in used_paths:
                    file_name = _safe_archive_name(f"{item.image_id}_{base_name}", f"{item.image_id}_{src.name}")
                    arcname = f"{root_name}/{pet_folder}/{file_name}"
                used_paths.add(arcname)
                zf.write(src, arcname)
                written += 1

    if written == 0:
        raise HTTPException(status_code=404, detail="No image files available for zip export")

    return FileResponse(
        path=zip_path,
        media_type='application/zip',
        filename=f"{root_name}.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )
