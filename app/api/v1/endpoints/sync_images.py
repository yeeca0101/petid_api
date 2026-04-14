"""페이스뱅크 이미지 동기화 조회/업로드를 처리하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.sync_images import SyncImagesQueryResponse, SyncImagesUploadResponse

router = APIRouter()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_folder_name(name: Optional[str]) -> str:
    if not name:
        return "unknown"
    safe = name.strip().replace("/", "_")
    if os.altsep:
        safe = safe.replace(os.altsep, "_")
    return safe or "unknown"


def _facebank_root(pet_id: str, pet_name: Optional[str], facebank_id: str) -> Path:
    return (
        Path(settings.verification_storage_dir)
        / "pets"
        / pet_id
        / _safe_folder_name(pet_name)
        / "facebanks"
        / facebank_id
    )


def _legacy_facebank_root(pet_id: str, facebank_id: str) -> Path:
    # Backward-compat read path for legacy layout.
    return Path(settings.storage_dir) / "pets" / pet_id / facebank_id


def _facebank_dir(pet_id: str, pet_name: Optional[str], facebank_id: str, version: int) -> Path:
    return _facebank_root(pet_id, pet_name, facebank_id) / f"v{version}"


def _latest_facebank_version(roots: Sequence[Path]) -> Optional[int]:
    versions: List[int] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.iterdir():
            if p.is_dir() and p.name.startswith("v"):
                try:
                    versions.append(int(p.name[1:]))
                except ValueError:
                    continue
    return max(versions) if versions else None


def _index_path(base_dir: Path) -> Path:
    return base_dir / "hash_index.json"


def _meta_path(base_dir: Path) -> Path:
    return base_dir / "facebank_meta.json"


def _load_index(base_dir: Path) -> Dict[str, dict]:
    path = _index_path(base_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read hash index: {e}") from e


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _safe_ext(filename: Optional[str]) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return ext
    return ".jpg"


def _parse_hashes_csv(hashes: str) -> List[str]:
    if not hashes:
        return []
    return [h.strip() for h in hashes.split(",") if h.strip()]


@router.get("/sync-images", response_model=SyncImagesQueryResponse)
def sync_images_query(
    pet_id: str = Query(..., alias="petId"),
    pet_name: Optional[str] = Query(default=None, alias="petName"),
    facebank_id: str = Query(..., alias="facebankId"),
    hashes: str = Query(..., description="CSV list of sha256 hashes"),
    facebank_version: Optional[int] = Query(default=None, alias="facebankVersion"),
):
    roots: List[Path] = []
    if pet_name is not None:
        roots.append(_facebank_root(pet_id, pet_name, facebank_id))
    roots.append(_legacy_facebank_root(pet_id, facebank_id))

    if pet_name is None:
        pet_root = Path(settings.verification_storage_dir) / "pets" / pet_id
        if pet_root.exists():
            for child in pet_root.iterdir():
                fb = child / "facebanks" / facebank_id
                roots.append(fb)

    if facebank_version is None:
        facebank_version = _latest_facebank_version(roots)
        if facebank_version is None:
            return SyncImagesQueryResponse(existing_hashes=[])

    base_dir = _facebank_dir(pet_id, pet_name, facebank_id, facebank_version)
    if not base_dir.exists():
        base_dir = _legacy_facebank_root(pet_id, facebank_id) / f"v{facebank_version}"
    index = _load_index(base_dir)
    requested = set(_parse_hashes_csv(hashes))
    existing = [h for h in requested if h in index]
    return SyncImagesQueryResponse(existing_hashes=existing)


@router.post("/sync-images", response_model=SyncImagesUploadResponse)
async def sync_images_upload(
    pet_id: str = Form(..., alias="petId"),
    pet_name: str = Form(..., alias="petName"),
    facebank_id: str = Form(..., alias="facebankId"),
    facebank_version: int = Form(..., alias="facebankVersion"),
    images: List[UploadFile] = File(...),
    hashes: List[str] = Form(...),
    model_version: Optional[str] = Form(default=None, alias="modelVersion"),
    embedding_dim: Optional[int] = Form(default=None, alias="embeddingDim"),
    threshold: Optional[float] = Form(default=None),
    device_id: Optional[str] = Form(default=None, alias="deviceId"),
    created_at: Optional[str] = Form(default=None, alias="createdAt"),
):
    if len(images) != len(hashes):
        raise HTTPException(status_code=400, detail="images and hashes count mismatch")

    base_dir = _facebank_dir(pet_id, pet_name, facebank_id, facebank_version)
    images_dir = base_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    index = _load_index(base_dir)
    existing_hashes = []
    stored = 0
    skipped = 0

    for upload, sha in zip(images, hashes):
        if sha in index:
            existing_hashes.append(sha)
            skipped += 1
            continue

        data = await upload.read()
        if len(data) > settings.max_image_bytes:
            raise HTTPException(status_code=413, detail=f"Image too large: {len(data)} bytes")

        ext = _safe_ext(upload.filename)
        filename = f"{sha[:12]}{ext}"
        target = images_dir / filename

        await run_in_threadpool(target.write_bytes, data)

        index[sha] = {
            "filename": filename,
            "original_name": upload.filename,
            "uploaded_at": _utcnow_iso(),
        }
        stored += 1

    await run_in_threadpool(_write_json, _index_path(base_dir), index)

    meta_path = _meta_path(base_dir)
    if not meta_path.exists():
        meta = {
            "pet_id": pet_id,
            "pet_name": pet_name,
            "facebank_id": facebank_id,
            "facebank_version": int(facebank_version),
            "created_at": created_at or _utcnow_iso(),
            "device_id": device_id,
            "model_version": model_version,
            "embedding_dim": embedding_dim,
            "threshold": threshold,
        }
        await run_in_threadpool(_write_json, meta_path, meta)

    return SyncImagesUploadResponse(
        pet_id=pet_id,
        facebank_id=facebank_id,
        facebank_version=int(facebank_version),
        received=len(images),
        skipped=skipped,
        stored=stored,
        existing_hashes=existing_hashes,
        model_version=model_version,
        embedding_dim=embedding_dim,
        threshold=threshold,
        device_id=device_id,
    )
