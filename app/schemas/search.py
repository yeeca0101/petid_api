from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.ingest import BBox


class SearchQuery(BaseModel):
    instance_ids: Optional[List[str]] = Field(
        default=None,
        description="One or more instance_ids to use as exemplar query vectors.",
    )
    merge: Literal["MAX", "RRF"] = Field(
        default="RRF",
        description="How to merge results when multiple exemplar vectors are provided.",
    )


class SearchFilters(BaseModel):
    species: Optional[str] = Field(default=None, description="DOG or CAT")
    captured_from: Optional[datetime] = None
    captured_to: Optional[datetime] = None


class SearchRequest(BaseModel):
    query: SearchQuery
    filters: Optional[SearchFilters] = None
    top_k_images: int = Field(default=200, ge=1, le=2000)
    per_query_limit: int = Field(default=400, ge=10, le=5000)


class BestMatch(BaseModel):
    instance_id: str
    bbox: Optional[BBox] = None
    score: float


class SearchResultItem(BaseModel):
    image_id: str
    score: float
    best_match: BestMatch


class SearchResponse(BaseModel):
    query_debug: dict
    results: List[SearchResultItem]
