from __future__ import annotations

from datetime import datetime
from typing import Literal, List, Optional

from pydantic import BaseModel, Field


class BBox(BaseModel):
    x1: float = Field(..., ge=0.0, le=1.0)
    y1: float = Field(..., ge=0.0, le=1.0)
    x2: float = Field(..., ge=0.0, le=1.0)
    y2: float = Field(..., ge=0.0, le=1.0)


class ImageMeta(BaseModel):
    image_id: str
    image_role: Literal["DAILY", "SEED"] = "DAILY"
    captured_at: Optional[datetime] = None
    uploaded_at: datetime
    width: int
    height: int
    storage_path: str


class EmbeddingMeta(BaseModel):
    embedding_type: str = Field(default="BODY")
    dim: int
    dtype: str = Field(default="float32")
    l2_normalized: bool = Field(default=True)
    model_version: str


class InstanceOut(BaseModel):
    instance_id: str
    class_id: int
    species: str
    confidence: float
    bbox: BBox
    pet_id: Optional[str] = None
    embedding: Optional[List[float]] = None
    embedding_meta: Optional[EmbeddingMeta] = None


class IngestResponse(BaseModel):
    image: ImageMeta
    instances: List[InstanceOut]
