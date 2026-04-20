from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import Settings
from app.db.models.reid import ImageRecord, InstanceRecord, JobRecord
from app.db.session import DatabaseManager


@dataclass(frozen=True)
class FilesystemSnapshot:
    pet_registry_exists: bool
    pet_registry_count: int
    meta_file_count: int
    meta_instance_count: int
    missing_raw_path_count: int
    missing_thumb_path_count: int


@dataclass(frozen=True)
class DatabaseSnapshot:
    image_count: int
    instance_count: int
    images_ready_count: int
    images_failed_count: int
    instances_ready_count: int
    instances_pending_count: int
    jobs_queued_count: int
    jobs_leased_count: int
    jobs_running_count: int
    jobs_failed_count: int
    stale_job_count: int
    queue_image_state_mismatch_count: int


@dataclass(frozen=True)
class ReconciliationReport:
    generated_at: str
    filesystem: FilesystemSnapshot
    database: DatabaseSnapshot
    parity: dict[str, int]
    cutover_checks: dict[str, bool]
    notes: list[str]


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_filesystem_snapshot(settings: Settings) -> FilesystemSnapshot:
    reid_root = Path(settings.reid_storage_dir)
    registry_path = reid_root / "registry" / "pets.json"
    registry_data = _read_json(registry_path) if registry_path.exists() else None
    pet_registry_count = len(registry_data) if isinstance(registry_data, dict) else 0

    meta_dir = reid_root / "meta"
    meta_files = sorted(meta_dir.glob("img_*.json")) if meta_dir.exists() else []
    meta_instance_count = 0
    missing_raw_path_count = 0
    missing_thumb_path_count = 0

    for meta_path in meta_files:
        payload = _read_json(meta_path)
        if not isinstance(payload, dict):
            continue
        instances = payload.get("instances") or []
        if isinstance(instances, list):
            meta_instance_count += len(instances)

        image = payload.get("image") or {}
        raw_path = Path(str(image.get("raw_path") or "").strip()) if str(image.get("raw_path") or "").strip() else None
        thumb_path = Path(str(image.get("thumb_path") or "").strip()) if str(image.get("thumb_path") or "").strip() else None
        if raw_path is not None and not raw_path.exists():
            missing_raw_path_count += 1
        if thumb_path is not None and not thumb_path.exists():
            missing_thumb_path_count += 1

    return FilesystemSnapshot(
        pet_registry_exists=registry_path.exists(),
        pet_registry_count=pet_registry_count,
        meta_file_count=len(meta_files),
        meta_instance_count=meta_instance_count,
        missing_raw_path_count=missing_raw_path_count,
        missing_thumb_path_count=missing_thumb_path_count,
    )


def build_database_snapshot(db: DatabaseManager) -> DatabaseSnapshot:
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=db.settings.queue_lease_timeout_s)
    with db.session_scope() as session:
        image_count = int(session.scalar(select(func.count()).select_from(ImageRecord)) or 0)
        instance_count = int(session.scalar(select(func.count()).select_from(InstanceRecord)) or 0)
        images_ready_count = int(
            session.scalar(select(func.count()).select_from(ImageRecord).where(ImageRecord.ingest_status == "READY")) or 0
        )
        images_failed_count = int(
            session.scalar(select(func.count()).select_from(ImageRecord).where(ImageRecord.ingest_status == "FAILED")) or 0
        )
        instances_ready_count = int(
            session.scalar(select(func.count()).select_from(InstanceRecord).where(InstanceRecord.vector_status == "READY")) or 0
        )
        instances_pending_count = int(
            session.scalar(select(func.count()).select_from(InstanceRecord).where(InstanceRecord.vector_status != "READY")) or 0
        )
        jobs_queued_count = int(
            session.scalar(select(func.count()).select_from(JobRecord).where(JobRecord.status == "QUEUED")) or 0
        )
        jobs_leased_count = int(
            session.scalar(select(func.count()).select_from(JobRecord).where(JobRecord.status == "LEASED")) or 0
        )
        jobs_running_count = int(
            session.scalar(select(func.count()).select_from(JobRecord).where(JobRecord.status == "RUNNING")) or 0
        )
        jobs_failed_count = int(
            session.scalar(
                select(func.count()).select_from(JobRecord).where(JobRecord.status.in_(("FAILED", "DEAD_LETTER")))
            )
            or 0
        )
        stale_job_count = int(
            session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.status.in_(("LEASED", "RUNNING")),
                    JobRecord.heartbeat_at.is_not(None),
                    JobRecord.heartbeat_at < stale_before,
                )
            )
            or 0
        )
        queue_image_state_mismatch_count = int(
            session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .join(ImageRecord, ImageRecord.image_id == JobRecord.payload["image_id"].astext, isouter=True)
                .where(
                    JobRecord.job_type == "INGEST_PIPELINE",
                    JobRecord.status.in_(("QUEUED", "LEASED", "RUNNING")),
                    ImageRecord.ingest_status == "READY",
                )
            )
            or 0
        )

    return DatabaseSnapshot(
        image_count=image_count,
        instance_count=instance_count,
        images_ready_count=images_ready_count,
        images_failed_count=images_failed_count,
        instances_ready_count=instances_ready_count,
        instances_pending_count=instances_pending_count,
        jobs_queued_count=jobs_queued_count,
        jobs_leased_count=jobs_leased_count,
        jobs_running_count=jobs_running_count,
        jobs_failed_count=jobs_failed_count,
        stale_job_count=stale_job_count,
        queue_image_state_mismatch_count=queue_image_state_mismatch_count,
    )


def build_reconciliation_report(db: DatabaseManager) -> ReconciliationReport:
    fs = build_filesystem_snapshot(db.settings)
    dbs = build_database_snapshot(db)
    notes: list[str] = []
    if fs.missing_raw_path_count or fs.missing_thumb_path_count:
        notes.append("filesystem contains metadata rows whose raw/thumb assets are missing")
    if dbs.stale_job_count:
        notes.append("queue has stale leased/running jobs past lease timeout")
    if dbs.queue_image_state_mismatch_count:
        notes.append("some queued/leased/running ingest jobs point to images already marked READY")
    notes.append("current schema does not yet include pets/assignments tables; parity is limited to images/instances/jobs")
    notes.append("current reconciliation report does not scan Qdrant contents directly; vector parity remains DB- and status-based")

    parity = {
        "image_count_delta": dbs.image_count - fs.meta_file_count,
        "instance_count_delta": dbs.instance_count - fs.meta_instance_count,
    }
    cutover_checks = {
        "filesystem_assets_present": fs.missing_raw_path_count == 0 and fs.missing_thumb_path_count == 0,
        "image_count_matches_meta": parity["image_count_delta"] == 0,
        "instance_count_matches_meta": parity["instance_count_delta"] == 0,
        "queue_drained": (dbs.jobs_queued_count + dbs.jobs_leased_count + dbs.jobs_running_count) == 0,
        "no_failed_jobs": dbs.jobs_failed_count == 0,
        "no_stale_jobs": dbs.stale_job_count == 0,
        "no_queue_image_state_mismatch": dbs.queue_image_state_mismatch_count == 0,
    }

    return ReconciliationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        filesystem=fs,
        database=dbs,
        parity=parity,
        cutover_checks=cutover_checks,
        notes=notes,
    )


def report_to_dict(report: ReconciliationReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "filesystem": asdict(report.filesystem),
        "database": asdict(report.database),
        "parity": report.parity,
        "cutover_checks": report.cutover_checks,
        "notes": report.notes,
    }
