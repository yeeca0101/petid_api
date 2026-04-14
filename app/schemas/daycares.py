from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class DaycareListItem(BaseModel):
    daycare_id: str
    image_count: int = 0
    instance_count: int = 0
    seed_image_count: int = 0
    daily_image_count: int = 0
    pet_count: int = 0
    last_captured_at: Optional[datetime] = None


class DaycaresListResponse(BaseModel):
    count: int
    items: List[DaycareListItem]
