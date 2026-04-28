from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api.v1.endpoints.admin import queue_summary


class FakeRepo:
    def count_jobs_by_status(self):
        return {"QUEUED": 2}

    def list_jobs(self, *, limit: int = 500, status=None):
        return [
            SimpleNamespace(
                job_id="job_1",
                status="QUEUED",
                heartbeat_at=datetime.now(timezone.utc),
            )
        ]


class FakeDb:
    @contextmanager
    def session_scope(self):
        yield object()


class AdminQueueSummaryBatchConfigTest(unittest.TestCase):
    def test_queue_summary_exposes_batch_runtime_settings(self) -> None:
        fake_settings = SimpleNamespace(
            enable_postgres_queue=True,
            queue_poll_interval_ms=1000,
            queue_lease_timeout_s=120,
            queue_local_capacity=8,
            scheduler_max_inflight_jobs=1,
            scheduler_enable_micro_batching=False,
            ingest_batch_pipeline_mode="batch_full",
            ingest_pipeline_slots=2,
            ingest_pipeline_local_queue_capacity=4,
            ingest_job_batch_size=8,
            ingest_job_batch_max_wait_ms=100,
            detector_batch_size=8,
            embedder_crop_batch_size=32,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=FakeDb())))

        with patch("app.api.v1.endpoints.admin.settings", fake_settings), patch(
            "app.api.v1.endpoints.admin.ReIdRepository",
            return_value=FakeRepo(),
        ):
            result = asyncio.run(queue_summary(request))

        self.assertEqual(result["ingest_batch_pipeline_mode"], "batch_full")
        self.assertEqual(result["ingest_job_batch_size"], 8)
        self.assertEqual(result["detector_batch_size"], 8)
        self.assertEqual(result["embedder_crop_batch_size"], 32)
        self.assertEqual(result["effective_ingest_images_in_gpu_path"], 16)


if __name__ == "__main__":
    unittest.main()
