from __future__ import annotations

import logging

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import DatabaseManager
from app.worker.pipeline import build_worker_resources, execute_ingest_pipeline
from app.worker.queue import QueueWorker, QueueWorkerConfig, build_default_worker_id
from app.worker.scheduler import build_v1_scheduler

logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging(settings.log_level)

    if not settings.enable_postgres_queue:
        logger.error("Queue worker cannot start because ENABLE_POSTGRES_QUEUE is false.")
        return 1

    scheduler = build_v1_scheduler(
        device=settings.device,
        local_queue_capacity=settings.queue_local_capacity,
        enable_micro_batching=settings.scheduler_enable_micro_batching,
        max_batch_items=settings.scheduler_max_batch_items,
        max_batch_wait_ms=settings.scheduler_max_batch_wait_ms,
    )
    scheduler.start()
    logger.info(
        "Worker scheduler ready | lane_id=%s | device=%s | local_queue_capacity=%s | micro_batching=%s",
        scheduler.policy.lane_id,
        scheduler.policy.device,
        scheduler.policy.local_queue_capacity,
        False,
    )

    db = DatabaseManager(settings)
    resources = build_worker_resources(settings)

    handlers = {
        "INGEST_PIPELINE": lambda *, job_id, payload: execute_ingest_pipeline(
            db=db,
            scheduler=scheduler,
            resources=resources,
            job_id=job_id,
            payload=payload,
        )
    }

    worker = QueueWorker(
        db=db,
        config=QueueWorkerConfig(
            worker_id=build_default_worker_id(),
            poll_interval_s=settings.queue_poll_interval_ms / 1000.0,
            lease_timeout_s=settings.queue_lease_timeout_s,
            heartbeat_interval_s=max(1.0, min(settings.queue_lease_timeout_s / 3.0, 10.0)),
        ),
        handlers=handlers,
    )
    try:
        worker.run_forever()
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
