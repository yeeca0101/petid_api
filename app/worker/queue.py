from __future__ import annotations

import logging
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from app.db.repositories import ReIdRepository
from app.db.session import DatabaseManager

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobHandler(Protocol):
    def __call__(self, *, job_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    job_type: str
    payload: dict[str, Any]
    leased_by: str


@dataclass(frozen=True)
class QueueWorkerConfig:
    worker_id: str
    poll_interval_s: float
    lease_timeout_s: int
    heartbeat_interval_s: float
    reaper_limit: int = 100


@dataclass(frozen=True)
class QueueWorkerResult:
    handled_job: bool
    claimed_job_id: uuid.UUID | None = None
    claimed_job_type: str | None = None
    reclaimed_count: int = 0


def build_default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


class QueueWorker:
    """Minimal durable queue worker with lease, heartbeat, and stale requeue support."""

    def __init__(
        self,
        *,
        db: DatabaseManager,
        config: QueueWorkerConfig,
        handlers: Mapping[str, JobHandler],
    ) -> None:
        self.db = db
        self.config = config
        self.handlers = dict(handlers)

    def run_forever(self) -> None:
        logger.info(
            "Queue worker starting | worker_id=%s | poll_interval_s=%s | lease_timeout_s=%s",
            self.config.worker_id,
            self.config.poll_interval_s,
            self.config.lease_timeout_s,
        )
        while True:
            result = self.run_once()
            if not result.handled_job:
                time.sleep(self.config.poll_interval_s)

    def run_once(self) -> QueueWorkerResult:
        reclaimed_count = self.reap_stale_jobs()
        claim = self.claim_next_job()
        if claim is None:
            return QueueWorkerResult(handled_job=False, reclaimed_count=reclaimed_count)

        self.process_claimed_job(claim)
        return QueueWorkerResult(
            handled_job=True,
            claimed_job_id=claim.job_id,
            claimed_job_type=claim.job_type,
            reclaimed_count=reclaimed_count,
        )

    def reap_stale_jobs(self) -> int:
        stale_before = _utcnow() - timedelta(seconds=self.config.lease_timeout_s)
        with self.db.session_scope() as session:
            repo = ReIdRepository(session)
            jobs = repo.requeue_stale_jobs(stale_before=stale_before, limit=self.config.reaper_limit)
            for job in jobs:
                repo.append_job_event(
                    job_id=job.job_id,
                    event_type="JOB_REQUEUED_AFTER_STALE_LEASE" if job.status == "QUEUED" else "JOB_DEAD_LETTERED_AFTER_STALE_LEASE",
                    payload={"worker_id": self.config.worker_id, "retry_count": job.retry_count},
                )
            return len(jobs)

    def claim_next_job(self, *, worker_id: str | None = None) -> ClaimedJob | None:
        claim_worker_id = worker_id or self.config.worker_id
        with self.db.session_scope() as session:
            repo = ReIdRepository(session)
            job = repo.claim_next_job(worker_id=claim_worker_id)
            if job is None:
                return None
            repo.append_job_event(
                job_id=job.job_id,
                event_type="JOB_LEASED",
                payload={"worker_id": claim_worker_id},
            )
            return ClaimedJob(
                job_id=job.job_id,
                job_type=job.job_type,
                payload=dict(job.payload),
                leased_by=claim_worker_id,
            )

    def process_claimed_job(self, claim: ClaimedJob, *, worker_id: str | None = None) -> None:
        exec_worker_id = worker_id or self.config.worker_id
        job_id = claim.job_id
        job_type = claim.job_type
        payload = claim.payload
        handler = self.handlers.get(job_type)
        if handler is None:
            self._fail_without_handler(job_id=job_id, job_type=job_type, worker_id=exec_worker_id)
            return

        self._mark_running(job_id=job_id, worker_id=exec_worker_id)
        stop_event = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            kwargs={"job_id": job_id, "stop_event": stop_event, "worker_id": exec_worker_id},
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = handler(job_id=job_id, payload=payload) or {}
        except Exception as exc:
            logger.exception("Queue job failed | worker_id=%s | job_id=%s | job_type=%s", exec_worker_id, job_id, job_type)
            self._mark_failed(
                job_id=job_id,
                error_code="JOB_HANDLER_ERROR",
                error_message="".join(traceback.format_exception_only(type(exc), exc)).strip(),
                worker_id=exec_worker_id,
            )
        else:
            self._mark_succeeded(job_id=job_id, result=result, worker_id=exec_worker_id)
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=self.config.heartbeat_interval_s + 1.0)

    def _mark_running(self, *, job_id: uuid.UUID, worker_id: str | None = None) -> None:
        event_worker_id = worker_id or self.config.worker_id
        with self.db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.start_job_run(job_id, worker_id=event_worker_id)
            repo.append_job_event(
                job_id=job_id,
                event_type="JOB_STARTED",
                payload={"worker_id": event_worker_id},
            )

    def _mark_succeeded(self, *, job_id: uuid.UUID, result: dict[str, Any], worker_id: str | None = None) -> None:
        event_worker_id = worker_id or self.config.worker_id
        with self.db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.complete_job(job_id, result=result)
            repo.append_job_event(
                job_id=job_id,
                event_type="JOB_SUCCEEDED",
                payload={"worker_id": event_worker_id},
            )

    def _mark_failed(
        self,
        *,
        job_id: uuid.UUID,
        error_code: str,
        error_message: str,
        worker_id: str | None = None,
    ) -> None:
        event_worker_id = worker_id or self.config.worker_id
        with self.db.session_scope() as session:
            repo = ReIdRepository(session)
            repo.fail_job(job_id, error_code=error_code, error_message=error_message)
            repo.append_job_event(
                job_id=job_id,
                event_type="JOB_FAILED",
                payload={
                    "worker_id": event_worker_id,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )

    def _fail_without_handler(self, *, job_id: uuid.UUID, job_type: str, worker_id: str | None = None) -> None:
        self._mark_failed(
            job_id=job_id,
            error_code="NO_JOB_HANDLER",
            error_message=f"No queue handler registered for job_type={job_type}",
            worker_id=worker_id,
        )

    def _heartbeat_loop(
        self,
        *,
        job_id: uuid.UUID,
        stop_event: threading.Event,
        worker_id: str | None = None,
    ) -> None:
        heartbeat_worker_id = worker_id or self.config.worker_id
        while not stop_event.wait(self.config.heartbeat_interval_s):
            try:
                with self.db.session_scope() as session:
                    repo = ReIdRepository(session)
                    repo.touch_job_heartbeat(job_id, heartbeat_at=_utcnow())
            except Exception:
                logger.exception("Queue heartbeat update failed | worker_id=%s | job_id=%s", heartbeat_worker_id, job_id)
