from __future__ import annotations

import logging
import sys

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import DatabaseManager
from app.worker.queue import QueueWorker, QueueWorkerConfig, build_default_worker_id

logger = logging.getLogger(__name__)


def main() -> int:
    setup_logging(settings.log_level)

    if not settings.enable_postgres_queue:
        logger.error("Queue worker cannot start because ENABLE_POSTGRES_QUEUE is false.")
        return 1

    handlers = {}
    if not handlers:
        logger.error(
            "Queue worker has no registered job handlers yet. "
            "Slice 6 installs the durable lease loop; pipeline execution handlers arrive in a later slice."
        )
        return 1

    worker = QueueWorker(
        db=DatabaseManager(settings),
        config=QueueWorkerConfig(
            worker_id=build_default_worker_id(),
            poll_interval_s=settings.queue_poll_interval_ms / 1000.0,
            lease_timeout_s=settings.queue_lease_timeout_s,
            heartbeat_interval_s=max(1.0, min(settings.queue_lease_timeout_s / 3.0, 10.0)),
        ),
        handlers=handlers,
    )
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
