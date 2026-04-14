from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from app.schemas.ingest import BBox


class IdentifyCandidate(BaseModel):
    pet_id: str
    pet_name: Optional[str] = None
    score: float


class IdentifyResponse(BaseModel):
    image_id: str
    instance_id: str
    species: str
    bbox: BBox
    candidates: List[IdentifyCandidate]
