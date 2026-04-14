from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class LabelAssignment(BaseModel):
    instance_id: str
    pet_id: Optional[str] = None
    action: Literal["ACCEPT", "REJECT", "CLEAR"] = "ACCEPT"
    reason: Optional[str] = None
    source: Literal["MANUAL", "AUTO", "PROPAGATED"] = "MANUAL"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class LabelRequest(BaseModel):
    labeled_by: Optional[str] = None
    assignments: List[LabelAssignment]


class LabelResponseItem(BaseModel):
    instance_id: str
    pet_id: Optional[str] = None
    assignment_status: Literal["UNREVIEWED", "ACCEPTED", "REJECTED"]
    updated: bool


class LabelResponse(BaseModel):
    labeled_at: datetime
    items: List[LabelResponseItem]
