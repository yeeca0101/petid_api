"""단일/배치 이미지 임베딩 생성을 제공하는 엔드포인트 모듈."""

from __future__ import annotations

import struct
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.embedding import BatchEmbeddingResponse, BatchEmbeddingItem, EmbeddingResponse
from app.utils.image_io import load_pil_image

router = APIRouter()


def _get_embedder(request: Request, profile: str):
    normalized = profile.strip().lower()
    if normalized not in ("verification", "reid"):
        raise HTTPException(status_code=400, detail=f"Invalid profile: {profile}")

    embedders = getattr(request.app.state, "embedders", None)
    if isinstance(embedders, dict):
        embedder = embedders.get(normalized)
        if embedder is not None:
            return embedder

    # Backward compatibility.
    embedder = getattr(request.app.state, "embedder", None)
    if embedder is None:
        raise HTTPException(status_code=503, detail="Model not ready")
    return embedder


@router.post("/embed", response_model=EmbeddingResponse)
async def embed_one(
    request: Request,
    file: UploadFile = File(...),
    profile: str = Query(
        default="verification",
        description="Embedding profile: verification|reid",
    ),
    format: Optional[str] = Query(
        default=None,
        description="Response format: json|f32|f16 (default from server settings)",
    ),
):
    """Generate an L2-normalized embedding for a single image."""

    embedder = _get_embedder(request, profile=profile)
    fmt = (format or settings.response_format).lower()
    if fmt not in ("json", "f32", "f16"):
        raise HTTPException(status_code=400, detail=f"Invalid format: {fmt}")

    img = await load_pil_image(file, settings.max_image_bytes)

    async with embedder.semaphore:
        emb = await run_in_threadpool(embedder.embed_one, img)

    # emb: np.ndarray shape (D,)
    dim = int(emb.shape[0])

    if fmt == "json":
        return EmbeddingResponse(
            model_version=embedder.model_info.model_version,
            dim=dim,
            embedding=emb.astype(np.float32).tolist(),
        )

    # Binary response
    if fmt == "f16":
        payload = emb.astype("<f2").tobytes()  # little-endian float16
        dtype = "float16"
    else:
        payload = emb.astype("<f4").tobytes()  # little-endian float32
        dtype = "float32"

    headers = {
        "X-Embedding-Dim": str(dim),
        "X-Embedding-DType": dtype,
        "X-Model-Version": embedder.model_info.model_version,
    }
    return Response(content=payload, media_type="application/octet-stream", headers=headers)


@router.post("/embed/batch", response_model=BatchEmbeddingResponse)
async def embed_batch(
    request: Request,
    files: List[UploadFile] = File(...),
    profile: str = Query(
        default="verification",
        description="Embedding profile: verification|reid",
    ),
    format: Optional[str] = Query(
        default=None,
        description="Response format: json|f32|f16 (default from server settings)",
    ),
):
    """Generate embeddings for a batch of images.

    Notes:
      - For PoC we support JSON response by default.
      - For higher throughput, use format=f16 or f32 (binary framed response).
    """

    embedder = _get_embedder(request, profile=profile)
    fmt = (format or settings.response_format).lower()
    if fmt not in ("json", "f32", "f16"):
        raise HTTPException(status_code=400, detail=f"Invalid format: {fmt}")

    if len(files) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files: {len(files)} > max_batch_size={settings.max_batch_size}",
        )

    images = []
    filenames = []
    for f in files:
        img = await load_pil_image(f, settings.max_image_bytes)
        images.append(img)
        filenames.append(f.filename)

    async with embedder.semaphore:
        embs = await run_in_threadpool(embedder.embed_pil_images, images)

    if embs.ndim != 2:
        raise HTTPException(status_code=500, detail=f"Unexpected embedding shape: {embs.shape}")

    n, dim = embs.shape

    if fmt == "json":
        items = [
            BatchEmbeddingItem(filename=filenames[i], embedding=embs[i].astype(np.float32).tolist())
            for i in range(n)
        ]
        return BatchEmbeddingResponse(
            model_version=embedder.model_info.model_version,
            dim=int(dim),
            items=items,
        )

    # Binary framed response (v1)
    # Layout:
    #   uint32 N
    #   uint32 D
    #   uint8  dtype_code (1=float32, 2=float16)
    #   embeddings bytes (row-major)
    if fmt == "f16":
        dtype_code = 2
        payload = embs.astype("<f2").tobytes()
        dtype = "float16"
    else:
        dtype_code = 1
        payload = embs.astype("<f4").tobytes()
        dtype = "float32"

    header = struct.pack("<IIB", int(n), int(dim), int(dtype_code))
    headers = {
        "X-Embedding-Count": str(int(n)),
        "X-Embedding-Dim": str(int(dim)),
        "X-Embedding-DType": dtype,
        "X-Model-Version": embedder.model_info.model_version,
        "X-Batch-Format": "dogface-batch-v1",
    }
    return Response(content=header + payload, media_type="application/octet-stream", headers=headers)
