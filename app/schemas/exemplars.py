from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ExemplarCreateItem(BaseModel):
    instance_id: str
    pet_id: str
    note: Optional[str] = None
    rank: Optional[int] = Field(default=None, ge=1, le=1000)
    active: bool = True
    sync_label: bool = True


class ExemplarCreateRequest(BaseModel):
    updated_by: Optional[str] = None
    items: List[ExemplarCreateItem] = Field(min_length=1, max_length=200)


class ExemplarUpdateRequest(BaseModel):
    updated_by: Optional[str] = None
    pet_id: Optional[str] = None
    note: Optional[str] = None
    rank: Optional[int] = Field(default=None, ge=1, le=1000)
    active: Optional[bool] = None
    sync_label: bool = True
    clear_note: bool = False


class ExemplarItem(BaseModel):
    instance_id: str
    image_id: Optional[str] = None
    img_name: Optional[str] = None
    species: Optional[str] = None
    pet_id: str
    active: bool
    rank: Optional[int] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    synced_label_pet_id: Optional[str] = None
    synced_assignment_status: Optional[str] = None


class ExemplarListResponse(BaseModel):
    count: int
    items: List[ExemplarItem]


class ExemplarMutationResponse(BaseModel):
    updated_at: datetime
    count: int
    items: List[ExemplarItem]


class ExemplarQuickRegisterResponse(BaseModel):
    mode: Literal["create", "append"]
    pet_id: str
    pet_name: str
    image_id: str
    updated_at: datetime
    count: int
    items: List[ExemplarItem]
    message: Optional[str] = None


class ExemplarFolderUploadItemResult(BaseModel):
    relative_path: str
    pet_name: Optional[str] = None
    pet_id: Optional[str] = None
    image_id: Optional[str] = None
    img_name: Optional[str] = None
    registered_instances: int = 0
    status: str
    error: Optional[str] = None


class ExemplarMoveToDailyRequest(BaseModel):
    mode: Literal["UNCLASSIFIED", "ACCEPTED"]
    updated_by: Optional[str] = None
    target_date: Optional[date] = None


class ExemplarMoveToDailyResponse(BaseModel):
    status: Literal["ok"] = "ok"
    instance_id: str
    image_id: Optional[str] = None
    from_role: Literal["SEED"] = "SEED"
    to_role: Literal["DAILY"] = "DAILY"
    assignment_status: Literal["UNREVIEWED", "ACCEPTED"]
    pet_id: Optional[str] = None
    updated_at: datetime


class ExemplarFolderUploadResponse(BaseModel):
    updated_at: datetime
    total_files: int
    succeeded: int
    failed: int
    results: List[ExemplarFolderUploadItemResult]
    message: Optional[str] = None
