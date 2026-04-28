from __future__ import annotations

import unittest

import numpy as np
from PIL import Image

from app.ml.cropper import NormalizedBBox
from app.worker.batch_types import (
    BatchCropRecord,
    BatchEmbeddingRecord,
    group_crop_records_by_batch_index,
    iter_chunks,
    validate_crop_indexes,
    validate_embedding_records,
)


class FakeDetection:
    class_id = 16
    confidence = 0.9
    x1 = 0.1
    y1 = 0.1
    x2 = 0.9
    y2 = 0.9


def _build_crop_records(counts: list[int]) -> list[BatchCropRecord]:
    records: list[BatchCropRecord] = []
    bbox = NormalizedBBox(x1=0.1, y1=0.1, x2=0.9, y2=0.9)
    crop = Image.new("RGB", (16, 16), (0, 0, 0))
    for batch_index, count in enumerate(counts):
        for image_local_index in range(count):
            records.append(
                BatchCropRecord(
                    crop_index=len(records),
                    batch_index=batch_index,
                    image_id=f"image_{batch_index + 1}",
                    source_detection_index=image_local_index,
                    selected_detection_index=image_local_index,
                    detection=FakeDetection(),
                    bbox=bbox,
                    crop=crop,
                )
            )
    return records


class IngestBatchLedgerTest(unittest.TestCase):
    def test_sixty_four_crops_chunk_and_regroup_without_losing_image_mapping(self) -> None:
        counts = [5, 11, 3, 8, 9, 10, 7, 11]
        records = _build_crop_records(counts)

        validate_crop_indexes(records)

        chunks = list(iter_chunks(records, 32))
        self.assertEqual(len(records), 64)
        self.assertEqual([len(chunk) for chunk in chunks], [32, 32])
        self.assertEqual([chunks[0][0].crop_index, chunks[0][-1].crop_index], [0, 31])
        self.assertEqual([chunks[1][0].crop_index, chunks[1][-1].crop_index], [32, 63])

        grouped = group_crop_records_by_batch_index(records)
        self.assertEqual([len(grouped[index]) for index in range(len(counts))], counts)

    def test_empty_detection_image_does_not_shift_downstream_indexes(self) -> None:
        counts = [0, 2, 0, 1]
        records = _build_crop_records(counts)

        validate_crop_indexes(records)

        grouped = group_crop_records_by_batch_index(records)
        self.assertNotIn(0, grouped)
        self.assertEqual([record.crop_index for record in grouped[1]], [0, 1])
        self.assertEqual([record.crop_index for record in grouped[3]], [2])

    def test_sixty_five_crops_create_final_partial_embed_chunk(self) -> None:
        records = _build_crop_records([65])

        chunks = list(iter_chunks(records, 32))

        self.assertEqual([len(chunk) for chunk in chunks], [32, 32, 1])
        self.assertEqual(chunks[-1][0].crop_index, 64)

    def test_embedding_records_are_validated_by_crop_index(self) -> None:
        records = _build_crop_records([2, 1])
        embeddings = [
            BatchEmbeddingRecord(crop_index=record.crop_index, vector=np.ones((4,), dtype=np.float32))
            for record in records
        ]

        validate_embedding_records(records, embeddings)

        with self.assertRaisesRegex(ValueError, "missing embeddings"):
            validate_embedding_records(records, embeddings[:-1])

        bad_dim = [
            BatchEmbeddingRecord(crop_index=0, vector=np.ones((4,), dtype=np.float32)),
            BatchEmbeddingRecord(crop_index=1, vector=np.ones((5,), dtype=np.float32)),
            BatchEmbeddingRecord(crop_index=2, vector=np.ones((4,), dtype=np.float32)),
        ]
        with self.assertRaisesRegex(ValueError, "embedding dimensions differ"):
            validate_embedding_records(records, bad_dim)


if __name__ == "__main__":
    unittest.main()
