from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.ml.detector import DetectedInstance
from app.worker.pipeline import execute_ingest_pipeline_batch
from app.worker.queue import ClaimedJob


@dataclass
class FakeImageRecord:
    image_id: str
    raw_path: str


class FakeRepo:
    def __init__(self, images: dict[str, FakeImageRecord]) -> None:
        self.images = images
        self.image_statuses: list[tuple[str, dict]] = []
        self.events: list[tuple[uuid.UUID, str, dict | None]] = []
        self.replaced_instances: dict[str, list[dict]] = {}

    def get_image(self, image_id: str):
        return self.images.get(image_id)

    def update_image_status(self, image_id: str, **kwargs):
        self.image_statuses.append((image_id, kwargs))

    def update_ingest_request_status(self, request_id: uuid.UUID, *, status: str, image_id: str | None = None):
        return None

    def append_job_event(self, *, job_id: uuid.UUID, event_type: str, payload: dict | None = None):
        self.events.append((job_id, event_type, payload))

    def replace_instances_for_image(self, image_id: str, *, instances: list[dict]):
        self.replaced_instances[image_id] = instances


class FakeDb:
    @contextmanager
    def session_scope(self):
        yield object()


class InlineScheduler:
    def __init__(self) -> None:
        self.submitted_tasks = []

    def submit(self, task, *, block: bool = False, timeout=None):
        self.submitted_tasks.append(task)
        return task.fn()


class CountingDetector:
    def __init__(self, counts: list[int]) -> None:
        self.counts = list(counts)
        self.offset = 0
        self.batch_sizes: list[int] = []

    def detect_batch(self, images):
        self.batch_sizes.append(len(images))
        groups = []
        for _image in images:
            count = self.counts[self.offset]
            self.offset += 1
            groups.append(
                [
                    DetectedInstance(
                        class_id=16,
                        confidence=0.9,
                        x1=0.1,
                        y1=0.1,
                        x2=0.9,
                        y2=0.9,
                    )
                    for _idx in range(count)
                ]
            )
        return groups


class CountingEmbedder:
    dim = 4
    model_info = SimpleNamespace(model_version="test-model")

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.next_value = 0

    def embed_pil_images(self, images):
        self.batch_sizes.append(len(images))
        rows = []
        for _image in images:
            rows.append(np.full((self.dim,), float(self.next_value), dtype=np.float32))
            self.next_value += 1
        return np.stack(rows, axis=0) if rows else np.zeros((0, self.dim), dtype=np.float32)


class FakeStore:
    def __init__(self) -> None:
        self.upserted = []

    def upsert(self, points):
        self.upserted.append(list(points))


def _settings():
    return SimpleNamespace(
        ingest_batch_pipeline_mode="batch_full",
        detector_batch_size=8,
        embedder_crop_batch_size=32,
        apply_min_bbox_area_to_seed=False,
        apply_min_bbox_area_to_daily=False,
        min_bbox_area_ratio=0.0,
        crop_padding=0.0,
    )


def _claim(job_id: uuid.UUID, image_id: str) -> ClaimedJob:
    return ClaimedJob(
        job_id=job_id,
        job_type="INGEST_PIPELINE",
        payload={"image_id": image_id, "image_role": "DAILY"},
        leased_by="worker:test",
    )


class IngestBatchAcceptanceTest(unittest.TestCase):
    def test_eight_image_sixty_four_crop_batch_acceptance(self) -> None:
        counts = [5, 11, 3, 8, 9, 10, 7, 11]
        image_ids = [f"img_{index}" for index in range(8)]
        job_ids = [uuid.uuid4() for _ in image_ids]
        repo = FakeRepo(
            {
                image_id: FakeImageRecord(image_id=image_id, raw_path=f"/tmp/{image_id}.jpg")
                for image_id in image_ids
            }
        )
        scheduler = InlineScheduler()
        detector = CountingDetector(counts)
        embedder = CountingEmbedder()
        store = FakeStore()
        resources = SimpleNamespace(settings=_settings(), detector=detector, embedder=embedder, store=store)

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo), patch(
            "app.worker.pipeline._load_pil_image_from_path",
            return_value=Image.new("RGB", (32, 32)),
        ), patch("app.worker.pipeline._write_meta_sidecar"):
            outcomes = execute_ingest_pipeline_batch(
                db=FakeDb(),
                scheduler=scheduler,
                resources=resources,
                claims=[_claim(job_id, image_id) for job_id, image_id in zip(job_ids, image_ids, strict=True)],
            )

        self.assertEqual(len(scheduler.submitted_tasks), 1)
        self.assertEqual(detector.batch_sizes, [8])
        self.assertEqual(embedder.batch_sizes, [32, 32])
        self.assertEqual(len(store.upserted), 1)
        self.assertEqual(len(store.upserted[0]), 64)
        self.assertEqual([outcomes[job_id]["instance_count"] for job_id in job_ids], counts)
        self.assertEqual([len(repo.replaced_instances[image_id]) for image_id in image_ids], counts)
        self.assertTrue(all(outcomes[job_id]["pipeline_stage"] == "READY" for job_id in job_ids))


if __name__ == "__main__":
    unittest.main()
