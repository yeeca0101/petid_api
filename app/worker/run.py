from __future__ import annotations

import logging
import queue
import threading
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import DatabaseManager
from app.worker.active_jobs import ActiveJobRegistry
from app.worker.pipeline import execute_ingest_pipeline, execute_ingest_pipeline_batch
from app.worker.queue import ClaimedJob, QueueWorker, QueueWorkerConfig, build_default_worker_id
from app.worker.scheduler import SingleLaneScheduler, build_v1_scheduler
from app.worker.slots import IngestPipelineSlot, build_ingest_pipeline_slot, build_ingest_pipeline_slots

logger = logging.getLogger(__name__)


def _heartbeat_interval_s() -> float:
    return max(1.0, min(settings.queue_lease_timeout_s / 3.0, 10.0))


def _use_batch_ingest_pipeline() -> bool:
    return settings.ingest_batch_pipeline_mode != "single"



def _build_scheduler() -> SingleLaneScheduler:
    return build_v1_scheduler(
        lane_id="lane-0",
        device=settings.device,
        local_queue_capacity=settings.queue_local_capacity,
        enable_micro_batching=settings.scheduler_enable_micro_batching,
        max_batch_items=settings.scheduler_max_batch_items,
        max_batch_wait_ms=settings.scheduler_max_batch_wait_ms,
    )


def _build_slot_worker(
    *,
    db: DatabaseManager,
    slot: IngestPipelineSlot,
    scheduler: SingleLaneScheduler,
) -> QueueWorker:
    handlers = {
        "INGEST_PIPELINE": lambda *, job_id, payload: execute_ingest_pipeline(
            db=db,
            scheduler=scheduler,
            resources=slot.resources,
            job_id=job_id,
            payload=payload,
        )
    }
    batch_handlers = {
        "INGEST_PIPELINE": lambda *, claims: execute_ingest_pipeline_batch(
            db=db,
            scheduler=scheduler,
            resources=slot.resources,
            claims=claims,
        )
    }
    return QueueWorker(
        db=db,
        config=QueueWorkerConfig(
            worker_id=slot.worker_id,
            poll_interval_s=settings.queue_poll_interval_ms / 1000.0,
            lease_timeout_s=settings.queue_lease_timeout_s,
            heartbeat_interval_s=_heartbeat_interval_s(),
        ),
        handlers=handlers,
        batch_handlers=batch_handlers,
    )


def _run_single_slot(*, db: DatabaseManager, base_worker_id: str) -> int:
    slot = build_ingest_pipeline_slot(
        settings=settings,
        base_worker_id=base_worker_id,
        slot_index=0,
    )
    logger.info(
        "Ingest slot initialized | slot_id=%s | worker_id=%s | detector_enabled=%s",
        slot.slot_id,
        slot.worker_id,
        slot.resources.detector is not None,
    )

    scheduler = _build_scheduler()
    scheduler.start()
    logger.info(
        "Worker scheduler ready | lane_id=%s | device=%s | local_queue_capacity=%s | micro_batching=%s",
        scheduler.policy.lane_id,
        scheduler.policy.device,
        scheduler.policy.local_queue_capacity,
        False,
    )

    worker = _build_slot_worker(db=db, slot=slot, scheduler=scheduler)
    try:
        if _use_batch_ingest_pipeline():
            logger.info(
                "Batch ingest worker loop enabled | mode=%s | job_batch_size=%s | max_wait_ms=%s | detector_batch_size=%s | embedder_crop_batch_size=%s",
                settings.ingest_batch_pipeline_mode,
                settings.ingest_job_batch_size,
                settings.ingest_job_batch_max_wait_ms,
                settings.detector_batch_size,
                settings.embedder_crop_batch_size,
            )
            while True:
                result = worker.run_once_batch(
                    job_type="INGEST_PIPELINE",
                    limit=settings.ingest_job_batch_size,
                    max_wait_s=settings.ingest_job_batch_max_wait_ms / 1000.0,
                )
                if not result.handled_job:
                    time.sleep(worker.config.poll_interval_s)
        else:
            worker.run_forever()
    finally:
        scheduler.stop()
    return 0


def _run_multi_slot(*, db: DatabaseManager, base_worker_id: str) -> int:
    slots = build_ingest_pipeline_slots(
        settings=settings,
        base_worker_id=base_worker_id,
        slot_count=settings.ingest_pipeline_slots,
    )
    schedulers = [
        build_v1_scheduler(
            lane_id=slot.slot_id,
            device=settings.device,
            local_queue_capacity=settings.queue_local_capacity,
            enable_micro_batching=settings.scheduler_enable_micro_batching,
            max_batch_items=settings.scheduler_max_batch_items,
            max_batch_wait_ms=settings.scheduler_max_batch_wait_ms,
        )
        for slot in slots
    ]
    slot_workers = [
        _build_slot_worker(db=db, slot=slot, scheduler=scheduler)
        for slot, scheduler in zip(slots, schedulers, strict=True)
    ]
    dispatch_queue: queue.Queue[ClaimedJob] = queue.Queue(
        maxsize=max(1, settings.ingest_pipeline_local_queue_capacity)
    )
    active_jobs = ActiveJobRegistry()

    for slot in slots:
        logger.info(
            "Ingest slot initialized | slot_id=%s | worker_id=%s | detector_enabled=%s",
            slot.slot_id,
            slot.worker_id,
            slot.resources.detector is not None,
        )

    for scheduler in schedulers:
        scheduler.start()
        logger.info(
            "Worker scheduler ready | lane_id=%s | device=%s | local_queue_capacity=%s | micro_batching=%s",
            scheduler.policy.lane_id,
            scheduler.policy.device,
            scheduler.policy.local_queue_capacity,
            False,
        )

    coordinator_worker_id = f"{base_worker_id}:coordinator"
    coordinator = QueueWorker(
        db=db,
        config=QueueWorkerConfig(
            worker_id=coordinator_worker_id,
            poll_interval_s=settings.queue_poll_interval_ms / 1000.0,
            lease_timeout_s=settings.queue_lease_timeout_s,
            heartbeat_interval_s=_heartbeat_interval_s(),
        ),
        handlers={},
    )
    occupancy_limit = settings.ingest_pipeline_slots + settings.ingest_pipeline_local_queue_capacity

    def log_active_job_summary(message: str) -> None:
        summary = active_jobs.summary()
        logger.info(
            "%s | occupancy=%s | claimed_jobs=%s | running_slots=%s | running_job_ids=%s",
            message,
            summary.occupancy_count,
            summary.claimed_job_count,
            ",".join(summary.running_slots) or "-",
            ",".join(summary.running_job_ids) or "-",
        )

    def slot_loop(slot: IngestPipelineSlot, worker: QueueWorker) -> None:
        while True:
            claim = dispatch_queue.get()
            try:
                claim = worker.adopt_claimed_job(claim, worker_id=slot.worker_id)
                active_jobs.mark_running(job_id=claim.job_id, slot_id=slot.slot_id, worker_id=slot.worker_id)
                logger.info(
                    "Slot worker executing claimed job | slot_id=%s | worker_id=%s | job_id=%s | leased_by=%s",
                    slot.slot_id,
                    slot.worker_id,
                    claim.job_id,
                    claim.leased_by,
                )
                log_active_job_summary("Active job registry updated after slot start")
                worker.process_claimed_job(claim, worker_id=slot.worker_id)
            except Exception:
                logger.exception(
                    "Slot worker loop recovered from unexpected error | slot_id=%s | worker_id=%s | job_id=%s",
                    slot.slot_id,
                    slot.worker_id,
                    claim.job_id,
                )
            finally:
                active_jobs.mark_finished(job_id=claim.job_id)
                log_active_job_summary("Active job registry updated after slot finish")
                dispatch_queue.task_done()

    slot_threads = [
        threading.Thread(
            target=slot_loop,
            args=(slot, worker),
            name=f"ingest-slot-{slot.slot_id}",
            daemon=True,
        )
        for slot, worker in zip(slots, slot_workers, strict=True)
    ]
    for thread in slot_threads:
        thread.start()

    logger.info(
        "Multi-slot ingest runtime ready | slot_count=%s | dispatch_queue_capacity=%s | occupancy_limit=%s",
        settings.ingest_pipeline_slots,
        settings.ingest_pipeline_local_queue_capacity,
        occupancy_limit,
    )

    try:
        while True:
            coordinator.reap_stale_jobs()
            current_summary = active_jobs.summary()
            if current_summary.occupancy_count >= occupancy_limit:
                time.sleep(coordinator.config.poll_interval_s)
                continue

            claim = coordinator.claim_next_job(worker_id=coordinator_worker_id)
            if claim is None:
                time.sleep(coordinator.config.poll_interval_s)
                continue

            active_jobs.register_claim(claim)
            try:
                dispatch_queue.put_nowait(claim)
            except queue.Full:
                active_jobs.release_claim(job_id=claim.job_id)
                logger.warning(
                    "Coordinator dispatch queue unexpectedly full after claim | job_id=%s | leased_by=%s",
                    claim.job_id,
                    claim.leased_by,
                )
                time.sleep(coordinator.config.poll_interval_s)
                continue
            logger.info(
                "Coordinator dispatched claimed job | job_id=%s | leased_by=%s | running_slots=%s | claimed_jobs=%s | occupancy=%s",
                claim.job_id,
                claim.leased_by,
                current_summary.running_slot_count,
                current_summary.claimed_job_count + 1,
                current_summary.occupancy_count + 1,
            )
            log_active_job_summary("Active job registry updated after dispatch")
    finally:
        for scheduler in schedulers:
            scheduler.stop()

    return 0


def main() -> int:
    setup_logging(settings.log_level)

    if not settings.enable_postgres_queue:
        logger.error("Queue worker cannot start because ENABLE_POSTGRES_QUEUE is false.")
        return 1

    logger.info(
        "Ingest slot config | ingest_pipeline_slots=%s | ingest_pipeline_local_queue_capacity=%s",
        settings.ingest_pipeline_slots,
        settings.ingest_pipeline_local_queue_capacity,
    )

    base_worker_id = build_default_worker_id()
    db = DatabaseManager(settings)

    if settings.ingest_pipeline_slots <= 1:
        return _run_single_slot(db=db, base_worker_id=base_worker_id)

    return _run_multi_slot(db=db, base_worker_id=base_worker_id)


if __name__ == "__main__":
    raise SystemExit(main())
