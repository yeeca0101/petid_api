from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.models.reid import ImageRecord, IngestRequestRecord, InstanceRecord, JobEventRecord, JobRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ReIdRepository:
    session: Session

    def create_image(
        self,
        *,
        image_id: str,
        image_role: str,
        uploaded_at: datetime,
        business_date,
        width: int,
        height: int,
        raw_path: str,
        thumb_path: str,
        daycare_id: Optional[str] = None,
        trainer_id: Optional[str] = None,
        input_pet_name: Optional[str] = None,
        captured_at: Optional[datetime] = None,
        original_filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        storage_state: str = "READY",
        ingest_status: str = "PENDING",
        pipeline_stage: Optional[str] = "STORED",
        pipeline_version: Optional[str] = None,
    ) -> ImageRecord:
        record = ImageRecord(
            image_id=image_id,
            image_role=image_role,
            daycare_id=daycare_id,
            trainer_id=trainer_id,
            input_pet_name=input_pet_name,
            captured_at=captured_at,
            uploaded_at=uploaded_at,
            business_date=business_date,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            width=width,
            height=height,
            raw_path=raw_path,
            thumb_path=thumb_path,
            storage_state=storage_state,
            pipeline_version=pipeline_version,
            ingest_status=ingest_status,
            pipeline_stage=pipeline_stage,
            updated_at=uploaded_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_image(self, image_id: str) -> ImageRecord | None:
        return self.session.get(ImageRecord, image_id)

    def update_image_status(
        self,
        image_id: str,
        *,
        ingest_status: Optional[str] = None,
        pipeline_stage: Optional[str] = None,
        last_error_code: Optional[str] = None,
        last_error_message: Optional[str] = None,
        storage_state: Optional[str] = None,
        source_detection_count: Optional[int] = None,
        primary_source_detection_index: Optional[int] = None,
    ) -> ImageRecord:
        record = self._require_image(image_id)
        if ingest_status is not None:
            record.ingest_status = ingest_status
        if pipeline_stage is not None:
            record.pipeline_stage = pipeline_stage
        if storage_state is not None:
            record.storage_state = storage_state
        if source_detection_count is not None:
            record.source_detection_count = source_detection_count
        if primary_source_detection_index is not None:
            record.primary_source_detection_index = primary_source_detection_index
        record.last_error_code = last_error_code
        record.last_error_message = last_error_message
        record.updated_at = _utcnow()
        self.session.flush()
        return record

    def replace_instances_for_image(
        self,
        image_id: str,
        *,
        instances: list[dict[str, Any]],
    ) -> list[InstanceRecord]:
        self._require_image(image_id)
        self.session.query(InstanceRecord).filter(InstanceRecord.image_id == image_id).delete(synchronize_session=False)
        records: list[InstanceRecord] = []
        for item in instances:
            record = InstanceRecord(
                instance_id=item["instance_id"],
                image_id=image_id,
                species=item["species"],
                class_id=item["class_id"],
                det_conf=item["det_conf"],
                bbox_x1=item["bbox_x1"],
                bbox_y1=item["bbox_y1"],
                bbox_x2=item["bbox_x2"],
                bbox_y2=item["bbox_y2"],
                qdrant_point_id=item.get("qdrant_point_id"),
                vector_status=item.get("vector_status", "READY"),
                embedding_type=item.get("embedding_type", "BODY"),
                model_version=item["model_version"],
                created_at=item.get("created_at", _utcnow()),
                updated_at=item.get("updated_at", _utcnow()),
            )
            self.session.add(record)
            records.append(record)
        self.session.flush()
        return records

    def create_ingest_request(
        self,
        *,
        idempotency_key: str,
        request_scope: str,
        status: str = "RECEIVED",
        image_id: Optional[str] = None,
        request_hash: Optional[str] = None,
        request_id: Optional[uuid.UUID] = None,
    ) -> IngestRequestRecord:
        now = _utcnow()
        record = IngestRequestRecord(
            request_id=request_id or uuid.uuid4(),
            idempotency_key=idempotency_key,
            request_scope=request_scope,
            image_id=image_id,
            request_hash=request_hash,
            status=status,
            created_at=now,
            updated_at=now,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_ingest_request(self, request_id: uuid.UUID) -> IngestRequestRecord | None:
        return self.session.get(IngestRequestRecord, request_id)

    def get_ingest_request_by_scope_key(self, *, request_scope: str, idempotency_key: str) -> IngestRequestRecord | None:
        stmt = select(IngestRequestRecord).where(
            IngestRequestRecord.request_scope == request_scope,
            IngestRequestRecord.idempotency_key == idempotency_key,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def update_ingest_request_status(
        self,
        request_id: uuid.UUID,
        *,
        status: str,
        image_id: Optional[str] = None,
    ) -> IngestRequestRecord:
        record = self._require_ingest_request(request_id)
        record.status = status
        if image_id is not None:
            record.image_id = image_id
        record.updated_at = _utcnow()
        self.session.flush()
        return record

    def enqueue_job(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        status: str = "QUEUED",
        dedupe_key: Optional[str] = None,
        priority: int = 100,
        max_retries: int = 3,
        available_at: Optional[datetime] = None,
        job_id: Optional[uuid.UUID] = None,
    ) -> JobRecord:
        record = JobRecord(
            job_id=job_id or uuid.uuid4(),
            job_type=job_type,
            dedupe_key=dedupe_key,
            status=status,
            priority=priority,
            payload=payload,
            max_retries=max_retries,
            available_at=available_at or _utcnow(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def append_job_event(
        self,
        *,
        job_id: uuid.UUID,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> JobEventRecord:
        record = JobEventRecord(job_id=job_id, event_type=event_type, payload=payload)
        self.session.add(record)
        self.session.flush()
        return record

    def get_job(self, job_id: uuid.UUID) -> JobRecord | None:
        return self.session.get(JobRecord, job_id)

    def get_job_by_request_id(self, request_id: uuid.UUID) -> JobRecord | None:
        stmt = (
            select(JobRecord)
            .where(JobRecord.payload["request_id"].astext == str(request_id))
            .order_by(JobRecord.created_at.desc())
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def update_job_status(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        locked_by: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        heartbeat_at: Optional[datetime] = None,
    ) -> JobRecord:
        record = self._require_job(job_id)
        record.status = status
        record.locked_by = locked_by if locked_by is not None else record.locked_by
        record.error_code = error_code
        record.error_message = error_message
        if result is not None:
            record.result = result
        if started_at is not None:
            record.started_at = started_at
        if finished_at is not None:
            record.finished_at = finished_at
        if heartbeat_at is not None:
            record.heartbeat_at = heartbeat_at
        self.session.flush()
        return record

    def start_job_run(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: Optional[str] = None,
        started_at: Optional[datetime] = None,
    ) -> JobRecord:
        current_time = started_at or _utcnow()
        record = self._require_job(job_id)
        record.status = "RUNNING"
        if worker_id is not None:
            record.locked_by = worker_id
        record.started_at = current_time
        record.heartbeat_at = current_time
        self.session.flush()
        return record

    def complete_job(
        self,
        job_id: uuid.UUID,
        *,
        result: Optional[dict[str, Any]] = None,
        finished_at: Optional[datetime] = None,
    ) -> JobRecord:
        current_time = finished_at or _utcnow()
        record = self._require_job(job_id)
        record.status = "SUCCEEDED"
        record.result = result
        record.finished_at = current_time
        record.heartbeat_at = current_time
        self.session.flush()
        return record

    def fail_job(
        self,
        job_id: uuid.UUID,
        *,
        error_code: Optional[str],
        error_message: Optional[str],
        finished_at: Optional[datetime] = None,
    ) -> JobRecord:
        current_time = finished_at or _utcnow()
        record = self._require_job(job_id)
        record.status = "FAILED"
        record.error_code = error_code
        record.error_message = error_message
        record.finished_at = current_time
        record.heartbeat_at = current_time
        self.session.flush()
        return record

    def cancel_job(
        self,
        job_id: uuid.UUID,
        *,
        finished_at: Optional[datetime] = None,
        error_message: Optional[str] = None,
    ) -> JobRecord:
        current_time = finished_at or _utcnow()
        record = self._require_job(job_id)
        record.status = "CANCELLED"
        record.error_message = error_message
        record.finished_at = current_time
        record.heartbeat_at = current_time
        self.session.flush()
        return record

    def touch_job_heartbeat(self, job_id: uuid.UUID, *, heartbeat_at: Optional[datetime] = None) -> JobRecord:
        record = self._require_job(job_id)
        record.heartbeat_at = heartbeat_at or _utcnow()
        self.session.flush()
        return record

    def claim_next_job(
        self,
        *,
        worker_id: str,
        job_type: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> JobRecord | None:
        current_time = now or _utcnow()
        stmt: Select[tuple[JobRecord]] = (
            select(JobRecord)
            .where(
                JobRecord.status == "QUEUED",
                JobRecord.available_at <= current_time,
            )
            .order_by(JobRecord.priority.asc(), JobRecord.available_at.asc(), JobRecord.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        if job_type is not None:
            stmt = stmt.where(JobRecord.job_type == job_type)

        record = self.session.execute(stmt).scalar_one_or_none()
        if record is None:
            return None

        record.status = "LEASED"
        record.locked_by = worker_id
        record.locked_at = current_time
        record.heartbeat_at = current_time
        self.session.flush()
        return record

    def requeue_stale_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> list[JobRecord]:
        stmt: Select[tuple[JobRecord]] = (
            select(JobRecord)
            .where(
                JobRecord.status.in_(("LEASED", "RUNNING")),
                JobRecord.heartbeat_at.is_not(None),
                JobRecord.heartbeat_at < stale_before,
            )
            .order_by(JobRecord.heartbeat_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        records = list(self.session.execute(stmt).scalars())
        for record in records:
            record.retry_count += 1
            record.available_at = _utcnow()
            record.locked_by = None
            record.locked_at = None
            record.heartbeat_at = None
            record.started_at = None
            record.finished_at = None
            record.error_code = "LEASE_TIMEOUT"
            record.error_message = "Job lease expired before completion"
            if record.retry_count > record.max_retries:
                record.status = "DEAD_LETTER"
            else:
                record.status = "QUEUED"
        self.session.flush()
        return records

    def _require_image(self, image_id: str) -> ImageRecord:
        record = self.get_image(image_id)
        if record is None:
            raise LookupError(f"image not found: {image_id}")
        return record

    def _require_ingest_request(self, request_id: uuid.UUID) -> IngestRequestRecord:
        record = self.get_ingest_request(request_id)
        if record is None:
            raise LookupError(f"ingest request not found: {request_id}")
        return record

    def _require_job(self, job_id: uuid.UUID) -> JobRecord:
        record = self.get_job(job_id)
        if record is None:
            raise LookupError(f"job not found: {job_id}")
        return record
