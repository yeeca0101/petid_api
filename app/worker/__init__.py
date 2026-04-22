from __future__ import annotations

from importlib import import_module

__all__ = [
    "JobHandler",
    "ClaimedJob",
    "QueueWorker",
    "QueueWorkerConfig",
    "QueueWorkerResult",
    "LaneExecutionPolicy",
    "SchedulerFullError",
    "SchedulerTask",
    "SingleLaneScheduler",
    "build_v1_scheduler",
    "IngestPipelineSlot",
    "build_ingest_pipeline_slot",
    "build_ingest_pipeline_slots",
    "build_slot_worker_id",
]

_QUEUE_EXPORTS = {
    "JobHandler",
    "ClaimedJob",
    "QueueWorker",
    "QueueWorkerConfig",
    "QueueWorkerResult",
}
_SCHEDULER_EXPORTS = {
    "LaneExecutionPolicy",
    "SchedulerFullError",
    "SchedulerTask",
    "SingleLaneScheduler",
    "build_v1_scheduler",
}
_SLOT_EXPORTS = {
    "IngestPipelineSlot",
    "build_ingest_pipeline_slot",
    "build_ingest_pipeline_slots",
    "build_slot_worker_id",
}


def __getattr__(name: str):
    if name in _QUEUE_EXPORTS:
        return getattr(import_module("app.worker.queue"), name)
    if name in _SCHEDULER_EXPORTS:
        return getattr(import_module("app.worker.scheduler"), name)
    if name in _SLOT_EXPORTS:
        return getattr(import_module("app.worker.slots"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
