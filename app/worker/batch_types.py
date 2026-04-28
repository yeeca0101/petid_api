from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence, TYPE_CHECKING, TypeVar

import numpy as np
from PIL import Image

from app.ml.cropper import NormalizedBBox

if TYPE_CHECKING:
    from app.ml.detector import DetectedInstance

T = TypeVar("T")


@dataclass(frozen=True)
class BatchJobRecord:
    batch_index: int
    job_id: uuid.UUID
    image_id: str
    request_id: uuid.UUID | None
    image_role: str
    payload: dict[str, Any]
    raw_path: str
    pil_image: Image.Image
    width: int
    height: int


@dataclass(frozen=True)
class BatchDetectionRecord:
    batch_index: int
    image_id: str
    source_detection_index: int
    selected_detection_index: int | None
    detection: DetectedInstance
    bbox: NormalizedBBox


@dataclass(frozen=True)
class BatchCropRecord:
    crop_index: int
    batch_index: int
    image_id: str
    source_detection_index: int
    selected_detection_index: int
    detection: DetectedInstance
    bbox: NormalizedBBox
    crop: Image.Image


@dataclass(frozen=True)
class BatchEmbeddingRecord:
    crop_index: int
    vector: np.ndarray


def iter_chunks(items: Sequence[T], chunk_size: int) -> Iterable[Sequence[T]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    for start in range(0, len(items), chunk_size):
        yield items[start : start + chunk_size]


def group_crop_records_by_batch_index(
    crop_records: Iterable[BatchCropRecord],
) -> dict[int, list[BatchCropRecord]]:
    grouped: dict[int, list[BatchCropRecord]] = {}
    for record in crop_records:
        grouped.setdefault(record.batch_index, []).append(record)
    return grouped


def validate_crop_indexes(crop_records: Sequence[BatchCropRecord]) -> None:
    seen: set[int] = set()
    for expected_index, record in enumerate(crop_records):
        if record.crop_index in seen:
            raise ValueError(f"duplicate crop_index={record.crop_index}")
        seen.add(record.crop_index)
        if record.crop_index != expected_index:
            raise ValueError(
                f"non-contiguous crop_index at position={expected_index}: got {record.crop_index}"
            )


def validate_embedding_records(
    crop_records: Sequence[BatchCropRecord],
    embedding_records: Sequence[BatchEmbeddingRecord],
) -> None:
    expected = {record.crop_index for record in crop_records}
    actual = set()
    embedding_dim: int | None = None
    for record in embedding_records:
        if record.crop_index in actual:
            raise ValueError(f"duplicate embedding crop_index={record.crop_index}")
        actual.add(record.crop_index)
        if record.vector.ndim != 1:
            raise ValueError(f"embedding for crop_index={record.crop_index} must be 1-D")
        if embedding_dim is None:
            embedding_dim = int(record.vector.shape[0])
        elif int(record.vector.shape[0]) != embedding_dim:
            raise ValueError("embedding dimensions differ inside batch")

    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ValueError(f"missing embeddings for crop indexes: {sorted(missing)}")
    if extra:
        raise ValueError(f"unexpected embeddings for crop indexes: {sorted(extra)}")
