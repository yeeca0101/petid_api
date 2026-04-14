from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class EmbeddingResponse(BaseModel):
    model_version: str = Field(..., description="Model/version identifier")
    dim: int = Field(..., description="Embedding dimension")
    embedding: List[float] = Field(..., description="L2-normalized embedding")


class BatchEmbeddingItem(BaseModel):
    filename: Optional[str] = Field(None, description="Original upload filename")
    embedding: List[float]


class BatchEmbeddingResponse(BaseModel):
    model_version: str
    dim: int
    items: List[BatchEmbeddingItem]
