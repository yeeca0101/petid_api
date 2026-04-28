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
from app.worker.batch_types import BatchCropRecord
from app.worker.pipeline import _embed_batch_crop_records, _run_gpu_ingest_batch_steps


@dataclass
class FakeRepo:
    image_statuses: list
    events: list

    def update_image_status(self, image_id: str, **kwargs):
        self.image_statuses.append((image_id, kwargs))

    def append_job_event(self, *, job_id: uuid.UUID, event_type: str, payload: dict | None = None):
        self.events.append((job_id, event_type, payload))


class FakeDb:
    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo

    @contextmanager
    def session_scope(self):
        yield object()


class FakeDetector:
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
                        confidence=0.5 + (idx * 0.01),
                        x1=0.1,
                        y1=0.1,
                        x2=0.9,
                        y2=0.9,
                    )
                    for idx in range(count)
                ]
            )
        return groups


class FakeEmbedder:
    dim = 4
    model_info = SimpleNamespace(model_version="test-model")

    def __init__(self, *, wrong_count: bool = False) -> None:
        self.batch_sizes: list[int] = []
        self.next_value = 0
        self.wrong_count = wrong_count

    def embed_pil_images(self, images):
        self.batch_sizes.append(len(images))
        count = max(0, len(images) - 1) if self.wrong_count else len(images)
        rows = []
        for _ in range(count):
            rows.append(np.full((self.dim,), float(self.next_value), dtype=np.float32))
            self.next_value += 1
        return np.stack(rows, axis=0) if rows else np.zeros((0, self.dim), dtype=np.float32)


def _settings(*, detector_batch_size: int = 8, embedder_crop_batch_size: int = 32):
    return SimpleNamespace(
        ingest_batch_pipeline_mode="batch_full",
        detector_batch_size=detector_batch_size,
        embedder_crop_batch_size=embedder_crop_batch_size,
        apply_min_bbox_area_to_seed=False,
        apply_min_bbox_area_to_daily=False,
        min_bbox_area_ratio=0.0,
        crop_padding=0.0,
    )


def _records(count: int):
    from app.worker.batch_types import BatchJobRecord

    return [
        BatchJobRecord(
            batch_index=index,
            job_id=uuid.uuid4(),
            image_id=f"img_{index}",
            request_id=None,
            image_role="DAILY",
            payload={"image_id": f"img_{index}", "image_role": "DAILY"},
            raw_path=f"/tmp/img_{index}.jpg",
            pil_image=Image.new("RGB", (32, 32)),
            width=32,
            height=32,
        )
        for index in range(count)
    ]


class EmbedderChunkMappingTest(unittest.TestCase):
    def test_sixty_four_crops_are_embedded_in_two_chunks_and_restored_per_image(self) -> None:
        counts = [5, 11, 3, 8, 9, 10, 7, 11]
        repo = FakeRepo(image_statuses=[], events=[])
        embedder = FakeEmbedder()
        resources = SimpleNamespace(
            settings=_settings(),
            detector=FakeDetector(counts),
            embedder=embedder,
        )
        records = _records(len(counts))

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo):
            processed = _run_gpu_ingest_batch_steps(
                db=FakeDb(repo),
                resources=resources,
                batch_records=records,
            )

        self.assertEqual(embedder.batch_sizes, [32, 32])
        self.assertEqual([len(processed[record.job_id]["instances"]) for record in records], counts)
        self.assertEqual(sum(len(processed[record.job_id]["points"]) for record in records), 64)
        self.assertEqual(
            [processed[record.job_id]["points"][0].vector[0] if counts[index] else None for index, record in enumerate(records)],
            [0.0, 5.0, 16.0, 19.0, 27.0, 36.0, 46.0, 53.0],
        )

    def test_sixty_five_crops_create_final_partial_embed_chunk(self) -> None:
        repo = FakeRepo(image_statuses=[], events=[])
        embedder = FakeEmbedder()
        resources = SimpleNamespace(
            settings=_settings(),
            detector=FakeDetector([65]),
            embedder=embedder,
        )

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo):
            processed = _run_gpu_ingest_batch_steps(
                db=FakeDb(repo),
                resources=resources,
                batch_records=_records(1),
            )

        self.assertEqual(embedder.batch_sizes, [32, 32, 1])
        self.assertEqual(len(next(iter(processed.values()))["instances"]), 65)

    def test_empty_detection_image_preserves_result_slot_and_skips_missing_embeddings(self) -> None:
        counts = [0, 2, 0, 1]
        repo = FakeRepo(image_statuses=[], events=[])
        embedder = FakeEmbedder()
        resources = SimpleNamespace(
            settings=_settings(detector_batch_size=2, embedder_crop_batch_size=2),
            detector=FakeDetector(counts),
            embedder=embedder,
        )
        records = _records(len(counts))

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo):
            processed = _run_gpu_ingest_batch_steps(
                db=FakeDb(repo),
                resources=resources,
                batch_records=records,
            )

        self.assertEqual(embedder.batch_sizes, [2, 1])
        self.assertEqual([len(processed[record.job_id]["instances"]) for record in records], counts)
        self.assertEqual([processed[records[index].job_id]["source_detection_count"] for index in range(4)], counts)

    def test_embedder_wrong_vector_count_fails_fast(self) -> None:
        resources = SimpleNamespace(
            settings=_settings(embedder_crop_batch_size=2),
            embedder=FakeEmbedder(wrong_count=True),
        )
        crop = Image.new("RGB", (16, 16))
        crop_records = [
            BatchCropRecord(
                crop_index=index,
                batch_index=0,
                image_id="img_0",
                source_detection_index=index,
                selected_detection_index=index,
                detection=DetectedInstance(class_id=16, confidence=1.0, x1=0.1, y1=0.1, x2=0.9, y2=0.9),
                bbox=SimpleNamespace(x1=0.1, y1=0.1, x2=0.9, y2=0.9),
                crop=crop,
            )
            for index in range(2)
        ]

        with self.assertRaisesRegex(RuntimeError, "Embedder returned"):
            _embed_batch_crop_records(resources=resources, crop_records=crop_records)


if __name__ == "__main__":
    unittest.main()
