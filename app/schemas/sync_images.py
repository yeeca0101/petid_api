from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SyncImagesQueryResponse(BaseModel):
    existing_hashes: List[str] = Field(default_factory=list)


class SyncImagesUploadResponse(BaseModel):
    pet_id: str
    facebank_id: str
    facebank_version: int
    received: int
    skipped: int
    stored: int
    existing_hashes: List[str] = Field(default_factory=list)
    model_version: Optional[str] = None
    embedding_dim: Optional[int] = None
    threshold: Optional[float] = None
    device_id: Optional[str] = None
