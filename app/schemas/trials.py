from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class TrialUploadResponse(BaseModel):
    trial_id: str
    status: Literal["stored", "duplicate"]
    stored: bool
    storage_path: Optional[str] = None
    outcome: Optional[Literal["TP", "FP", "FN", "TN"]] = None
