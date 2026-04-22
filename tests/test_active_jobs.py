from __future__ import annotations

import unittest
import uuid
from dataclasses import dataclass

from app.worker.active_jobs import ActiveJobRegistry


@dataclass(frozen=True)
class FakeClaim:
    job_id: uuid.UUID
    job_type: str
    payload: dict
    leased_by: str


class ActiveJobRegistryTest(unittest.TestCase):
    def test_summary_tracks_claim_running_and_finish(self) -> None:
        registry = ActiveJobRegistry()
        claim = FakeClaim(
            job_id=uuid.uuid4(),
            job_type="INGEST_PIPELINE",
            payload={},
            leased_by="worker:coordinator",
        )

        registry.register_claim(claim)
        claimed_summary = registry.summary()
        self.assertEqual(claimed_summary.occupancy_count, 1)
        self.assertEqual(claimed_summary.claimed_job_count, 1)
        self.assertEqual(claimed_summary.running_slot_count, 0)
        self.assertEqual(claimed_summary.claimed_job_ids, (str(claim.job_id),))

        registry.mark_running(job_id=claim.job_id, slot_id="slot-0", worker_id="worker:slot-0")
        running_summary = registry.summary()
        self.assertEqual(running_summary.occupancy_count, 1)
        self.assertEqual(running_summary.claimed_job_count, 0)
        self.assertEqual(running_summary.running_slot_count, 1)
        self.assertEqual(running_summary.running_job_ids, (str(claim.job_id),))
        self.assertEqual(running_summary.running_slots, ("slot-0",))

        registry.mark_finished(job_id=claim.job_id)
        finished_summary = registry.summary()
        self.assertEqual(finished_summary.occupancy_count, 0)
        self.assertEqual(finished_summary.claimed_job_count, 0)
        self.assertEqual(finished_summary.running_slot_count, 0)

    def test_release_claim_clears_unstarted_job(self) -> None:
        registry = ActiveJobRegistry()
        claim = FakeClaim(
            job_id=uuid.uuid4(),
            job_type="INGEST_PIPELINE",
            payload={},
            leased_by="worker:coordinator",
        )

        registry.register_claim(claim)
        registry.release_claim(job_id=claim.job_id)

        summary = registry.summary()
        self.assertEqual(summary.occupancy_count, 0)
        self.assertEqual(summary.claimed_job_count, 0)
        self.assertEqual(summary.running_slot_count, 0)


if __name__ == "__main__":
    unittest.main()
