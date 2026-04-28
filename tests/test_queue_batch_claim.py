from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

from app.worker.queue import BatchJobFailure, ClaimedJob, QueueWorker, QueueWorkerConfig


@dataclass
class FakeJob:
    job_id: uuid.UUID
    job_type: str
    payload: dict


class FakeRepo:
    def __init__(self, jobs: list[FakeJob] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.events: list[tuple[uuid.UUID, str, dict | None]] = []
        self.started: list[uuid.UUID] = []
        self.succeeded: dict[uuid.UUID, dict] = {}
        self.failed: dict[uuid.UUID, tuple[str | None, str | None]] = {}
        self.touched: list[uuid.UUID] = []
        self.last_claim_args: dict | None = None

    def claim_next_jobs(self, *, worker_id: str, job_type: str, limit: int):
        self.last_claim_args = {"worker_id": worker_id, "job_type": job_type, "limit": limit}
        claimed = self.jobs[:limit]
        self.jobs = self.jobs[limit:]
        return claimed

    def requeue_stale_jobs(self, *, stale_before, limit: int):
        return []

    def append_job_event(self, *, job_id: uuid.UUID, event_type: str, payload: dict | None = None):
        self.events.append((job_id, event_type, payload))

    def start_job_run(self, job_id: uuid.UUID, *, worker_id: str | None = None):
        self.started.append(job_id)

    def complete_job(self, job_id: uuid.UUID, *, result: dict | None = None):
        self.succeeded[job_id] = result or {}

    def fail_job(self, job_id: uuid.UUID, *, error_code: str | None, error_message: str | None):
        self.failed[job_id] = (error_code, error_message)

    def touch_job_heartbeat(self, job_id: uuid.UUID, *, heartbeat_at=None):
        self.touched.append(job_id)


class FakeDb:
    @contextmanager
    def session_scope(self):
        yield object()


def _worker(repo: FakeRepo, *, batch_handlers=None) -> QueueWorker:
    worker = QueueWorker(
        db=FakeDb(),
        config=QueueWorkerConfig(
            worker_id="worker:test",
            poll_interval_s=0.001,
            lease_timeout_s=30,
            heartbeat_interval_s=60,
        ),
        handlers={},
        batch_handlers=batch_handlers,
    )
    worker._repo_patch = patch("app.worker.queue.ReIdRepository", return_value=repo)
    worker._repo_patch.start()
    return worker


def _stop_worker_patch(worker: QueueWorker) -> None:
    patcher = getattr(worker, "_repo_patch", None)
    if patcher is not None:
        patcher.stop()


class QueueBatchClaimTest(unittest.TestCase):
    def test_claim_next_jobs_returns_claims_and_records_batch_lease_events(self) -> None:
        jobs = [
            FakeJob(job_id=uuid.uuid4(), job_type="INGEST_PIPELINE", payload={"image_id": f"img_{idx}"})
            for idx in range(3)
        ]
        repo = FakeRepo(jobs)
        worker = _worker(repo)
        try:
            claims = worker.claim_next_jobs(job_type="INGEST_PIPELINE", limit=2)
        finally:
            _stop_worker_patch(worker)

        self.assertEqual(len(claims), 2)
        self.assertEqual(repo.last_claim_args, {"worker_id": "worker:test", "job_type": "INGEST_PIPELINE", "limit": 2})
        self.assertEqual([claim.job_id for claim in claims], [jobs[0].job_id, jobs[1].job_id])
        self.assertEqual([event[1] for event in repo.events], ["JOB_LEASED", "JOB_LEASED"])
        self.assertTrue(all(event[2] and event[2].get("batch_claim") is True for event in repo.events))

    def test_process_claimed_jobs_allows_per_job_success_and_failure(self) -> None:
        ok_id = uuid.uuid4()
        failed_id = uuid.uuid4()
        claims = [
            ClaimedJob(job_id=ok_id, job_type="INGEST_PIPELINE", payload={"image_id": "ok"}, leased_by="worker:test"),
            ClaimedJob(job_id=failed_id, job_type="INGEST_PIPELINE", payload={"image_id": "bad"}, leased_by="worker:test"),
        ]
        repo = FakeRepo()

        def handler(*, claims):
            return {
                ok_id: {"image_id": "ok", "instance_count": 3},
                failed_id: BatchJobFailure(error_code="IMAGE_FAILED", error_message="bad image"),
            }

        worker = _worker(repo, batch_handlers={"INGEST_PIPELINE": handler})
        try:
            with patch("app.worker.queue.logger.exception"):
                worker.process_claimed_jobs(claims)
        finally:
            _stop_worker_patch(worker)

        self.assertEqual(repo.started, [ok_id, failed_id])
        self.assertEqual(repo.succeeded, {ok_id: {"image_id": "ok", "instance_count": 3}})
        self.assertEqual(repo.failed, {failed_id: ("IMAGE_FAILED", "bad image")})
        self.assertEqual(
            [event[1] for event in repo.events],
            ["JOB_STARTED", "JOB_STARTED", "JOB_SUCCEEDED", "JOB_FAILED"],
        )

    def test_batch_handler_exception_fails_all_claims(self) -> None:
        claims = [
            ClaimedJob(job_id=uuid.uuid4(), job_type="INGEST_PIPELINE", payload={}, leased_by="worker:test"),
            ClaimedJob(job_id=uuid.uuid4(), job_type="INGEST_PIPELINE", payload={}, leased_by="worker:test"),
        ]
        repo = FakeRepo()

        def handler(*, claims):
            raise RuntimeError("boom")

        worker = _worker(repo, batch_handlers={"INGEST_PIPELINE": handler})
        try:
            with patch("app.worker.queue.logger.exception"):
                worker.process_claimed_jobs(claims)
        finally:
            _stop_worker_patch(worker)

        self.assertEqual(set(repo.failed), {claim.job_id for claim in claims})
        self.assertTrue(all(value[0] == "BATCH_JOB_HANDLER_ERROR" for value in repo.failed.values()))

    def test_run_once_batch_processes_partial_batch(self) -> None:
        jobs = [
            FakeJob(job_id=uuid.uuid4(), job_type="INGEST_PIPELINE", payload={"image_id": "img_1"}),
            FakeJob(job_id=uuid.uuid4(), job_type="INGEST_PIPELINE", payload={"image_id": "img_2"}),
        ]
        repo = FakeRepo(jobs)

        def handler(*, claims):
            return {claim.job_id: {"image_id": claim.payload["image_id"]} for claim in claims}

        worker = _worker(repo, batch_handlers={"INGEST_PIPELINE": handler})
        try:
            result = worker.run_once_batch(job_type="INGEST_PIPELINE", limit=8, max_wait_s=0)
        finally:
            _stop_worker_patch(worker)

        self.assertTrue(result.handled_job)
        self.assertEqual(result.claimed_job_ids, (jobs[0].job_id, jobs[1].job_id))
        self.assertEqual(set(repo.succeeded), {jobs[0].job_id, jobs[1].job_id})
        self.assertEqual(repo.jobs, [])


if __name__ == "__main__":
    unittest.main()
