from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AdminImageLabelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    image_ids: List[str] = Field(min_length=1)
    action: Literal["ACCEPT", "CLEAR", "REJECT"] = "ACCEPT"
    pet_id: Optional[str] = None
    target_date: Optional[date] = Field(default=None, alias="date")
    labeled_by: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: Literal["MANUAL", "AUTO", "PROPAGATED"] = "MANUAL"
    select_mode: Literal["BEST_CONFIDENCE", "ALL"] = "BEST_CONFIDENCE"


class AdminImageLabelItem(BaseModel):
    image_id: str
    selected_instance_ids: List[str]
    updated_count: int
    skipped_reason: Optional[str] = None


class AdminImageLabelResponse(BaseModel):
    action: Literal["ACCEPT", "CLEAR", "REJECT"]
    pet_id: Optional[str] = None
    labeled_at: datetime
    items: List[AdminImageLabelItem]
