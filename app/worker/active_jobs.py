from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.worker.queue import ClaimedJob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActiveJobRecord:
    job_id: uuid.UUID
    job_type: str
    leased_by: str
    slot_id: str | None
    worker_id: str | None
    state: str
    claimed_at: datetime
    started_at: datetime | None = None


class ActiveJobRegistry:
    """In-process slot runtime state for claimed and running jobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[uuid.UUID, ActiveJobRecord] = {}

    def register_claim(self, claim: ClaimedJob) -> None:
        claimed_at = _utcnow()
        with self._lock:
            self._jobs[claim.job_id] = ActiveJobRecord(
                job_id=claim.job_id,
                job_type=claim.job_type,
                leased_by=claim.leased_by,
                slot_id=None,
                worker_id=None,
                state="CLAIMED",
                claimed_at=claimed_at,
            )

    def mark_running(self, *, job_id: uuid.UUID, slot_id: str, worker_id: str) -> None:
        started_at = _utcnow()
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            self._jobs[job_id] = ActiveJobRecord(
                job_id=record.job_id,
                job_type=record.job_type,
                leased_by=record.leased_by,
                slot_id=slot_id,
                worker_id=worker_id,
                state="RUNNING",
                claimed_at=record.claimed_at,
                started_at=started_at,
            )

    def mark_finished(self, *, job_id: uuid.UUID) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            del self._jobs[job_id]

    def running_slot_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._jobs.values() if record.state == "RUNNING" and record.slot_id is not None)

    def snapshot(self) -> list[ActiveJobRecord]:
        with self._lock:
            return list(self._jobs.values())
