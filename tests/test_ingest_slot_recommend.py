from __future__ import annotations

import unittest

from app.tools.ingest_slot_recommend import CapacitySnapshot, recommend_slots


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


if __name__ == "__main__":
    unittest.main()
