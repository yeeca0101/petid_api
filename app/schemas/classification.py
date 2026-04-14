from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AutoClassifyRequest(BaseModel):
    date: date
    species: Optional[Literal["DOG", "CAT"]] = None
    auto_accept_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    candidate_threshold: float = Field(default=0.62, ge=0.0, le=1.0)
    search_limit: int = Field(default=200, ge=10, le=2000)
    labeled_by: Optional[str] = None
    dry_run: bool = False


class AutoClassifySummary(BaseModel):
    scanned_instances: int
    accepted: int
    unreviewed_candidate: int
    unreviewed_no_candidate: int
    unchanged: int


class AutoClassifyItem(BaseModel):
    instance_id: str
    image_id: str
    species: str
    score: Optional[float] = None
    selected_pet_id: Optional[str] = None
    assignment_status: Literal["UNREVIEWED", "ACCEPTED"]
    updated: bool


class AutoClassifyResponse(BaseModel):
    requested_at: datetime
    date: date
    dry_run: bool
    summary: AutoClassifySummary
    items: List[AutoClassifyItem]


class SimilarSearchRequest(BaseModel):
    date: date
    tab: Literal["ALL", "UNCLASSIFIED", "PET"] = "ALL"
    pet_id: Optional[str] = None
    include_seed: bool = False
    query_instance_ids: List[str] = Field(default_factory=list, min_length=1)
    merge: Literal["MAX", "RRF"] = "MAX"
    top_k_images: int = Field(default=200, ge=1, le=2000)
    per_query_limit: int = Field(default=400, ge=10, le=5000)


class SimilarSearchItem(BaseModel):
    image_id: str
    score: float
    best_match_instance_id: Optional[str] = None
    best_match_score: Optional[float] = None
    raw_url: Optional[str] = None
    thumb_url: Optional[str] = None


class SimilarSearchResponse(BaseModel):
    requested_at: datetime
    date: date
    tab: Literal["ALL", "UNCLASSIFIED", "PET"]
    pet_id: Optional[str] = None
    query_debug: dict
    results: List[SimilarSearchItem]


class FinalizeBucketsRequest(BaseModel):
    date: date
    pet_ids: Optional[List[str]] = None


class FinalizeBucketImageItem(BaseModel):
    image_id: str
    file_name: str
    original_filename: Optional[str] = None
    raw_path: str
    raw_url: Optional[str] = None
    captured_at: Optional[str] = None


class FinalizeBucketItem(BaseModel):
    pet_id: str
    pet_name: Optional[str] = None
    image_ids: List[str]
    images: List[FinalizeBucketImageItem] = Field(default_factory=list)
    count: int
    instance_count: int = 0


class BucketQualityMetrics(BaseModel):
    total_day_images: int
    unclassified_images: int
    unclassified_image_ratio: float
    total_instances: int
    accepted_instances: int
    accepted_auto_instances: int
    unreviewed_instances: int
    rejected_instances: int
    auto_accept_ratio: float


class FinalizeBucketsResponse(BaseModel):
    finalized_at: datetime
    date: date
    bucket_count: int
    total_images: int
    quality_metrics: BucketQualityMetrics
    manifest_path: str
    buckets: List[FinalizeBucketItem]


class GetBucketsResponse(BaseModel):
    date: date
    manifest_path: str
    finalized_at: datetime
    bucket_count: int
    total_images: int
    quality_metrics: BucketQualityMetrics
    buckets: List[FinalizeBucketItem]
