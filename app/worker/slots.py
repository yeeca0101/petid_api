from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.worker.pipeline import WorkerResources, build_worker_resources


@dataclass(frozen=True)
class IngestPipelineSlot:
    slot_index: int
    slot_id: str
    worker_id: str
    resources: WorkerResources


def build_slot_worker_id(base_worker_id: str, slot_index: int) -> str:
    return f"{base_worker_id}:slot-{slot_index}"


def build_ingest_pipeline_slot(
    *,
    settings: Settings,
    base_worker_id: str,
    slot_index: int,
) -> IngestPipelineSlot:
    return IngestPipelineSlot(
        slot_index=slot_index,
        slot_id=f"slot-{slot_index}",
        worker_id=build_slot_worker_id(base_worker_id, slot_index),
        resources=build_worker_resources(settings),
    )


def build_ingest_pipeline_slots(
    *,
    settings: Settings,
    base_worker_id: str,
    slot_count: int,
) -> list[IngestPipelineSlot]:
    return [
        build_ingest_pipeline_slot(
            settings=settings,
            base_worker_id=base_worker_id,
            slot_index=slot_index,
        )
        for slot_index in range(slot_count)
    ]

