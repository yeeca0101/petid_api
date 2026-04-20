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


class IngestAcceptedImage(BaseModel):
    image_id: str
    image_role: Literal["DAILY", "SEED"] = "DAILY"
    uploaded_at: datetime
    width: int
    height: int
    storage_path: str
    thumb_path: str
    ingest_status: Literal["PENDING", "PROCESSING", "READY", "FAILED"] = "PENDING"
    pipeline_stage: Optional[str] = "STORED"


class IngestAcceptedResponse(BaseModel):
    request_id: str
    job_id: str
    status_url: str
    image: IngestAcceptedImage


class IngestStatusResponse(BaseModel):
    request_id: str
    request_status: str
    image_id: Optional[str] = None
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    image_role: Optional[str] = None
    ingest_status: Optional[str] = None
    pipeline_stage: Optional[str] = None
    created_at: datetime
    updated_at: datetime
