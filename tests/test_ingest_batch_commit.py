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
    def __init__(self, images: dict[str, FakeImageRecord], *, fail_replace_for: set[str] | None = None) -> None:
        self.images = images
        self.fail_replace_for = set(fail_replace_for or set())
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
        if image_id in self.fail_replace_for:
            raise RuntimeError(f"replace failed for {image_id}")
        self.replaced_instances[image_id] = instances


class FakeDb:
    @contextmanager
    def session_scope(self):
        yield object()


class FakeScheduler:
    def __init__(self, result_by_job_id: dict[uuid.UUID, dict]) -> None:
        self.result_by_job_id = result_by_job_id

    def submit(self, task, *, block: bool = False, timeout=None):
        return self.result_by_job_id


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.upserted = []

    def upsert(self, points):
        if self.fail:
            raise RuntimeError("qdrant unavailable")
        self.upserted.append(list(points))


def _claim(job_id: uuid.UUID, image_id: str, request_id: uuid.UUID | None = None) -> ClaimedJob:
    payload = {"image_id": image_id, "image_role": "DAILY"}
    if request_id is not None:
        payload["request_id"] = str(request_id)
    return ClaimedJob(job_id=job_id, job_type="INGEST_PIPELINE", payload=payload, leased_by="worker:test")


def _processed(image_id: str, instance_count: int) -> dict:
    instances = []
    points = []
    for index in range(instance_count):
        point_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"{image_id}:instance:{index}")
        instance_id = f"ins_{point_uuid}"
        instances.append(
            {
                "instance_id": instance_id,
                "species": "DOG",
                "class_id": 16,
                "confidence": 0.9,
                "bbox": {"x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9},
            }
        )
        points.append(SimpleNamespace(id=str(point_uuid), payload={"instance_id": instance_id, "image_id": image_id}))
    return {
        "points": points,
        "instances": instances,
        "source_detections": [],
        "source_detection_count": instance_count,
        "primary_source_detection_index": None,
        "model_version": "test-model",
    }


class IngestBatchCommitTest(unittest.TestCase):
    def test_one_qdrant_upsert_and_per_image_db_rows_match_points(self) -> None:
        job_1 = uuid.uuid4()
        job_2 = uuid.uuid4()
        req_1 = uuid.uuid4()
        req_2 = uuid.uuid4()
        repo = FakeRepo(
            {
                "img_1": FakeImageRecord(image_id="img_1", raw_path="/tmp/img_1.jpg"),
                "img_2": FakeImageRecord(image_id="img_2", raw_path="/tmp/img_2.jpg"),
            }
        )
        store = FakeStore()
        scheduler = FakeScheduler({job_1: _processed("img_1", 2), job_2: _processed("img_2", 1)})
        resources = SimpleNamespace(settings=SimpleNamespace(), store=store)

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (16, 16)),
        ), patch("app.worker.pipeline._write_meta_sidecar"):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(job_1, "img_1", req_1), _claim(job_2, "img_2", req_2)],
            )

        self.assertEqual(outcomes[job_1]["instance_count"], 2)
        self.assertEqual(outcomes[job_2]["instance_count"], 1)
        self.assertEqual(len(store.upserted), 1)
        self.assertEqual(len(store.upserted[0]), 3)
        self.assertEqual(
            [row["instance_id"] for row in repo.replaced_instances["img_1"]],
            [point.payload["instance_id"] for point in store.upserted[0][:2]],
        )
        self.assertEqual(
            [row["instance_id"] for row in repo.replaced_instances["img_2"]],
            [store.upserted[0][2].payload["instance_id"]],
        )
        self.assertIn((req_1, "SUCCEEDED", "img_1"), repo.request_statuses)
        self.assertIn((req_2, "SUCCEEDED", "img_2"), repo.request_statuses)

    def test_qdrant_upsert_failure_marks_all_batch_images_failed(self) -> None:
        job_1 = uuid.uuid4()
        job_2 = uuid.uuid4()
        repo = FakeRepo(
            {
                "img_1": FakeImageRecord(image_id="img_1", raw_path="/tmp/img_1.jpg"),
                "img_2": FakeImageRecord(image_id="img_2", raw_path="/tmp/img_2.jpg"),
            }
        )
        scheduler = FakeScheduler({job_1: _processed("img_1", 1), job_2: _processed("img_2", 1)})
        resources = SimpleNamespace(settings=SimpleNamespace(), store=FakeStore(fail=True))

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (16, 16)),
        ):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(job_1, "img_1"), _claim(job_2, "img_2")],
            )

        self.assertIsInstance(outcomes[job_1], BatchJobFailure)
        self.assertIsInstance(outcomes[job_2], BatchJobFailure)
        self.assertEqual(outcomes[job_1].error_code, "INGEST_PIPELINE_UPSERT_ERROR")
        self.assertEqual(repo.replaced_instances, {})
        failed_updates = [
            (image_id, kwargs)
            for image_id, kwargs in repo.image_statuses
            if kwargs.get("pipeline_stage") == "FAILED"
        ]
        self.assertEqual({image_id for image_id, _kwargs in failed_updates}, {"img_1", "img_2"})

    def test_finalize_failure_is_isolated_to_one_image(self) -> None:
        good_job = uuid.uuid4()
        bad_job = uuid.uuid4()
        repo = FakeRepo(
            {
                "img_good": FakeImageRecord(image_id="img_good", raw_path="/tmp/img_good.jpg"),
                "img_bad": FakeImageRecord(image_id="img_bad", raw_path="/tmp/img_bad.jpg"),
            },
            fail_replace_for={"img_bad"},
        )
        scheduler = FakeScheduler({good_job: _processed("img_good", 1), bad_job: _processed("img_bad", 1)})
        resources = SimpleNamespace(settings=SimpleNamespace(), store=FakeStore())

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (16, 16)),
        ), patch("app.worker.pipeline._write_meta_sidecar"):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(good_job, "img_good"), _claim(bad_job, "img_bad")],
            )

        self.assertEqual(outcomes[good_job]["pipeline_stage"], "READY")
        self.assertIsInstance(outcomes[bad_job], BatchJobFailure)
        self.assertEqual(outcomes[bad_job].error_code, "INGEST_PIPELINE_FINALIZE_ERROR")
        self.assertIn("img_good", repo.replaced_instances)
        self.assertNotIn("img_bad", repo.replaced_instances)
        ready_updates = [
            image_id
            for image_id, kwargs in repo.image_statuses
            if kwargs.get("pipeline_stage") == "READY"
        ]
        failed_updates = [
            image_id
            for image_id, kwargs in repo.image_statuses
            if kwargs.get("pipeline_stage") == "FAILED"
        ]
        self.assertIn("img_good", ready_updates)
        self.assertIn("img_bad", failed_updates)


if __name__ == "__main__":
    unittest.main()
