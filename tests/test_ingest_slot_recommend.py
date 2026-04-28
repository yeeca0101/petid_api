from __future__ import annotations

import unittest

from app.tools.ingest_slot_recommend import CapacitySnapshot, RuntimeProbeUsage, recommend_batch_pipeline, recommend_slots


class IngestSlotRecommendTest(unittest.TestCase):
    def test_recommendation_uses_more_conservative_resource_limit(self) -> None:
        recommendation = recommend_slots(
            capacity=CapacitySnapshot(total_ram_gb=64.0, total_vram_gb=24.0, gpu_name="Test GPU"),
            per_slot_vram_gb=6.0,
            per_slot_ram_gb=8.0,
            safety_vram_gb=3.0,
            safety_ram_gb=4.0,
            minimum=1,
            maximum=32,
        )
        self.assertEqual(recommendation.ram_limited_slots, 7)
        self.assertEqual(recommendation.vram_limited_slots, 3)
        self.assertEqual(recommendation.recommended_slots, 3)

    def test_recommendation_falls_back_to_ram_when_gpu_is_unavailable(self) -> None:
        recommendation = recommend_slots(
            capacity=CapacitySnapshot(total_ram_gb=32.0, total_vram_gb=None, gpu_name=None),
            per_slot_vram_gb=6.0,
            per_slot_ram_gb=8.0,
            safety_vram_gb=3.0,
            safety_ram_gb=4.0,
            minimum=1,
            maximum=32,
        )
        self.assertEqual(recommendation.ram_limited_slots, 3)
        self.assertIsNone(recommendation.vram_limited_slots)
        self.assertEqual(recommendation.recommended_slots, 3)

    def test_batch_pipeline_recommendation_reports_new_settings(self) -> None:
        runtime_probe = RuntimeProbeUsage(
            measured_per_slot_ram_gb=4.0,
            measured_per_slot_vram_gb=5.0,
            build_peak_ram_delta_gb=1.0,
            build_peak_vram_delta_gb=2.0,
            probe_peak_ram_delta_gb=3.0,
            probe_peak_vram_delta_gb=3.0,
            detected_instances=9,
            cropped_instances=8,
            embedded_instances=8,
            embedding_dim=512,
            model_version="test",
            image_width=640,
            image_height=480,
            image_role="DAILY",
        )

        recommendation = recommend_batch_pipeline(
            recommended_slots=2,
            job_batch_size=8,
            detector_batch_size=16,
            embedder_crop_batch_size=32,
            runtime_probe=runtime_probe,
        )

        self.assertEqual(recommendation.mode, "batch_full")
        self.assertEqual(recommendation.job_batch_size, 8)
        self.assertEqual(recommendation.detector_batch_size, 8)
        self.assertEqual(recommendation.embedder_crop_batch_size, 32)
        self.assertEqual(recommendation.effective_images_in_gpu_path, 16)
        self.assertEqual(recommendation.estimated_crops_per_job_batch, 64)


if __name__ == "__main__":
    unittest.main()
