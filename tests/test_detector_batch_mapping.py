from __future__ import annotations

import unittest
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.ml.detector import DetectedInstance, YoloDetector
from app.worker.batch_types import BatchJobRecord
from app.worker.pipeline import _detect_batch_for_records, _run_gpu_ingest_steps


class FakeTensor:
    def __init__(self, values) -> None:
        self.values = np.asarray(values)

    def cpu(self):
        return self

    def numpy(self):
        return self.values


class FakeBoxes:
    def __init__(self, xyxy, conf, cls) -> None:
        self.xyxy = FakeTensor(xyxy)
        self.conf = FakeTensor(conf)
        self.cls = FakeTensor(cls)

    def __len__(self) -> int:
        return len(self.conf.values)


class FakeResult:
    def __init__(self, boxes) -> None:
        self.boxes = boxes


class FakeModel:
    def __init__(self, results) -> None:
        self.results = results
        self.sources_seen = []

    def predict(self, **kwargs):
        self.sources_seen.append(kwargs["source"])
        return self.results


class FakeDetector:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def detect_batch(self, images):
        self.batch_sizes.append(len(images))
        return [
            [DetectedInstance(class_id=16, confidence=float(index), x1=0.1, y1=0.1, x2=0.9, y2=0.9)]
            for index, _image in enumerate(images)
        ]


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


class FakeEmbedder:
    dim = 4
    model_info = SimpleNamespace(model_version="test-model")

    def embed_pil_images(self, images):
        return np.ones((len(images), 4), dtype=np.float32)


def _detector() -> YoloDetector:
    detector = YoloDetector.__new__(YoloDetector)
    detector.imgsz = 960
    detector.conf = 0.25
    detector.iou = 0.45
    detector.device = "cpu"
    detector.keep_class_ids = [15, 16]
    return detector


def _batch_record(index: int, job_id: uuid.UUID | None = None) -> BatchJobRecord:
    image = Image.new("RGB", (16, 16), (index, index, index))
    return BatchJobRecord(
        batch_index=index,
        job_id=job_id or uuid.uuid4(),
        image_id=f"img_{index}",
        request_id=None,
        image_role="DAILY",
        payload={"image_id": f"img_{index}", "image_role": "DAILY"},
        raw_path=f"/tmp/img_{index}.jpg",
        pil_image=image,
        width=image.size[0],
        height=image.size[1],
    )


class DetectorBatchMappingTest(unittest.TestCase):
    def test_detect_batch_preserves_input_order_and_empty_results(self) -> None:
        detector = _detector()
        detector.model = FakeModel(
            [
                FakeResult(FakeBoxes([[0, 0, 10, 10]], [0.8], [16])),
                FakeResult(None),
                FakeResult(FakeBoxes([[5, 5, 15, 15], [0, 0, 5, 5]], [0.7, 0.6], [16, 15])),
            ]
        )
        images = [
            Image.new("RGB", (20, 10)),
            Image.new("RGB", (30, 30)),
            Image.new("RGB", (10, 20)),
        ]

        detections = detector.detect_batch(images)

        self.assertEqual([len(group) for group in detections], [1, 0, 2])
        self.assertAlmostEqual(detections[0][0].x2, 0.5)
        self.assertAlmostEqual(detections[2][0].x1, 0.5)
        self.assertEqual(len(detector.model.sources_seen[0]), 3)

    def test_pipeline_detector_chunking_preserves_batch_indexes(self) -> None:
        records = [_batch_record(index) for index in range(5)]
        detector = FakeDetector()
        resources = SimpleNamespace(settings=SimpleNamespace(detector_batch_size=2), detector=detector)

        detections_by_job_id = _detect_batch_for_records(resources=resources, batch_records=records)

        self.assertEqual(detector.batch_sizes, [2, 2, 1])
        self.assertEqual(set(detections_by_job_id), {record.job_id for record in records})
        self.assertEqual([detections_by_job_id[record.job_id][0].confidence for record in records], [0.0, 1.0, 0.0, 1.0, 0.0])

    def test_seed_primary_detection_index_is_from_precomputed_source_detections(self) -> None:
        repo = FakeRepo(image_statuses=[], events=[])
        job_id = uuid.uuid4()
        resources = SimpleNamespace(
            settings=SimpleNamespace(
                apply_min_bbox_area_to_seed=False,
                apply_min_bbox_area_to_daily=False,
                min_bbox_area_ratio=0.0,
                crop_padding=0.0,
            ),
            detector=None,
            embedder=FakeEmbedder(),
        )
        detections = [
            DetectedInstance(class_id=16, confidence=0.99, x1=0.0, y1=0.0, x2=0.2, y2=0.2),
            DetectedInstance(class_id=16, confidence=0.50, x1=0.4, y1=0.4, x2=0.6, y2=0.6),
        ]

        with patch("app.worker.pipeline.ReIdRepository", return_value=repo):
            processed = _run_gpu_ingest_steps(
                db=FakeDb(repo),
                resources=resources,
                job_id=job_id,
                image_id="img_seed",
                pil_image=Image.new("RGB", (32, 32)),
                payload={"image_id": "img_seed", "image_role": "SEED"},
                precomputed_detections=detections,
            )

        self.assertEqual(processed["source_detection_count"], 2)
        self.assertEqual(processed["primary_source_detection_index"], 1)
        self.assertEqual(len(processed["instances"]), 1)


if __name__ == "__main__":
    unittest.main()
