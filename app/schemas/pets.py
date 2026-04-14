from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class PetListItem(BaseModel):
    pet_id: str
    pet_name: Optional[str] = None
    image_count: int = 0
    instance_count: int = 0


class PetsListResponse(BaseModel):
    count: int
    items: List[PetListItem]
