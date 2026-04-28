from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.worker.pipeline import execute_ingest_pipeline_batch
from app.worker.queue import BatchJobFailure, ClaimedJob


@dataclass
class FakeImageRecord:
    image_id: str
    raw_path: str


class FakeRepo:
    def __init__(self, images: dict[str, FakeImageRecord]) -> None:
        self.images = images
        self.image_statuses: list[tuple[str, dict]] = []
        self.request_statuses: list[tuple[uuid.UUID, str, str | None]] = []
        self.events: list[tuple[uuid.UUID, str, dict | None]] = []
        self.replaced_instances: dict[str, list[dict]] = {}

    def get_image(self, image_id: str):
        return self.images.get(image_id)

    def update_image_status(self, image_id: str, **kwargs):
        self.image_statuses.append((image_id, kwargs))

    def update_ingest_request_status(self, request_id: uuid.UUID, *, status: str, image_id: str | None = None):
        self.request_statuses.append((request_id, status, image_id))

    def append_job_event(self, *, job_id: uuid.UUID, event_type: str, payload: dict | None = None):
        self.events.append((job_id, event_type, payload))

    def replace_instances_for_image(self, image_id: str, *, instances: list[dict]):
        self.replaced_instances[image_id] = instances


class FakeDb:
    @contextmanager
    def session_scope(self):
        yield object()


class FakeScheduler:
    def __init__(self, result_by_job_id: dict[uuid.UUID, dict] | None = None) -> None:
        self.result_by_job_id = result_by_job_id or {}
        self.submitted_tasks = []

    def submit(self, task, *, block: bool = False, timeout=None):
        self.submitted_tasks.append(task)
        if self.result_by_job_id:
            return self.result_by_job_id
        return task.fn()


class FakeStore:
    def __init__(self) -> None:
        self.upserted = []

    def upsert(self, points):
        self.upserted.append(list(points))


def _processed(image_id: str, instance_count: int = 1) -> dict:
    instances = [
        {
            "instance_id": f"ins_{image_id}_{idx}",
            "species": "DOG",
            "class_id": 16,
            "confidence": 0.9,
            "bbox": {"x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9},
        }
        for idx in range(instance_count)
    ]
    return {
        "points": [object() for _ in range(instance_count)],
        "instances": instances,
        "source_detections": [],
        "source_detection_count": instance_count,
        "primary_source_detection_index": None,
        "model_version": "test-model",
    }


def _claim(job_id: uuid.UUID, image_id: str, request_id: uuid.UUID | None = None) -> ClaimedJob:
    payload = {"image_id": image_id, "image_role": "DAILY"}
    if request_id is not None:
        payload["request_id"] = str(request_id)
    return ClaimedJob(job_id=job_id, job_type="INGEST_PIPELINE", payload=payload, leased_by="worker:test")


class IngestBatchPipelineSkeletonTest(unittest.TestCase):
    def test_one_item_batch_returns_single_pipeline_shape(self) -> None:
        job_id = uuid.uuid4()
        request_id = uuid.uuid4()
        image_id = "img_1"
        repo = FakeRepo({image_id: FakeImageRecord(image_id=image_id, raw_path="/tmp/img_1.jpg")})
        scheduler = FakeScheduler({job_id: _processed(image_id, instance_count=2)})
        store = FakeStore()
        resources = SimpleNamespace(settings=SimpleNamespace(), store=store)

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (16, 16)),
        ), patch("app.worker.pipeline._write_meta_sidecar"):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(job_id, image_id, request_id)],
            )

        self.assertEqual(outcomes[job_id], {"image_id": image_id, "instance_count": 2, "pipeline_stage": "READY"})
        self.assertEqual(len(scheduler.submitted_tasks), 1)
        self.assertEqual(len(store.upserted), 1)
        self.assertEqual(len(store.upserted[0]), 2)
        self.assertEqual(len(repo.replaced_instances[image_id]), 2)

    def test_missing_image_fails_one_job_and_does_not_block_valid_job(self) -> None:
        missing_job_id = uuid.uuid4()
        valid_job_id = uuid.uuid4()
        valid_image_id = "img_valid"
        repo = FakeRepo({valid_image_id: FakeImageRecord(image_id=valid_image_id, raw_path="/tmp/img_valid.jpg")})
        scheduler = FakeScheduler({valid_job_id: _processed(valid_image_id)})
        resources = SimpleNamespace(settings=SimpleNamespace(), store=FakeStore())

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (16, 16)),
        ), patch("app.worker.pipeline._write_meta_sidecar"):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[
                    _claim(missing_job_id, "img_missing"),
                    _claim(valid_job_id, valid_image_id),
                ],
            )

        self.assertIsInstance(outcomes[missing_job_id], BatchJobFailure)
        self.assertEqual(outcomes[missing_job_id].error_code, "INGEST_PIPELINE_PREFLIGHT_ERROR")
        self.assertEqual(outcomes[valid_job_id]["image_id"], valid_image_id)
        self.assertEqual(len(scheduler.submitted_tasks), 1)
        self.assertEqual(scheduler.submitted_tasks[0].payload["image_ids"], [valid_image_id])

    def test_empty_model_batch_after_preflight_failures_does_not_submit_scheduler(self) -> None:
        job_id = uuid.uuid4()
        repo = FakeRepo({})
        scheduler = FakeScheduler()
        resources = SimpleNamespace(settings=SimpleNamespace(), store=FakeStore())

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(job_id, "img_missing")],
            )

        self.assertIsInstance(outcomes[job_id], BatchJobFailure)
        self.assertEqual(len(scheduler.submitted_tasks), 0)


if __name__ == "__main__":
    unittest.main()
