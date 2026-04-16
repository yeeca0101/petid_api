from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ImageRecord(Base):
    __tablename__ = "images"
    __table_args__ = (
        CheckConstraint("image_role in ('DAILY', 'SEED')", name="images_image_role_valid"),
        CheckConstraint(
            "storage_state in ('PENDING', 'READY', 'MISSING', 'DELETED')",
            name="images_storage_state_valid",
        ),
        CheckConstraint(
            "ingest_status in ('PENDING', 'PROCESSING', 'READY', 'FAILED')",
            name="images_ingest_status_valid",
        ),
        CheckConstraint(
            "pipeline_stage is null or pipeline_stage in "
            "('RECEIVED', 'STORED', 'WAITING_FOR_SCHEDULER', 'DETECTING', "
            "'CROPPING', 'EMBEDDING', 'UPSERTING_VECTOR', 'FINALIZING', "
            "'READY', 'FAILED', 'CANCELLED')",
            name="images_pipeline_stage_valid",
        ),
        Index("images_business_date_idx", "business_date"),
        Index("images_role_date_idx", "image_role", "business_date"),
        Index("images_daycare_date_idx", "daycare_id", "business_date"),
        Index("images_ingest_status_idx", "ingest_status", "business_date"),
    )

    image_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_role: Mapped[str] = mapped_column(String, nullable=False)
    daycare_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trainer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_pet_name: Mapped[str | None] = mapped_column(String, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_path: Mapped[str] = mapped_column(Text, nullable=False)
    thumb_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_state: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'READY'"))
    pipeline_version: Mapped[str | None] = mapped_column(String, nullable=True)
    source_detection_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary_source_detection_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingest_status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'PENDING'"))
    pipeline_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    instances: Mapped[list["InstanceRecord"]] = relationship(back_populates="image", cascade="all, delete-orphan")


class InstanceRecord(Base):
    __tablename__ = "instances"
    __table_args__ = (
        CheckConstraint(
            "vector_status in ('PENDING', 'READY', 'FAILED', 'DELETED')",
            name="instances_vector_status_valid",
        ),
        Index("instances_image_id_idx", "image_id"),
        Index("instances_species_idx", "species"),
        Index("instances_vector_status_idx", "vector_status"),
        Index(
            "instances_qdrant_point_id_uq",
            "qdrant_point_id",
            unique=True,
            postgresql_where=text("qdrant_point_id is not null"),
        ),
    )

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.image_id", ondelete="CASCADE"), nullable=False)
    species: Mapped[str] = mapped_column(String, nullable=False)
    class_id: Mapped[int] = mapped_column(Integer, nullable=False)
    det_conf: Mapped[float] = mapped_column(nullable=False)
    bbox_x1: Mapped[float] = mapped_column(nullable=False)
    bbox_y1: Mapped[float] = mapped_column(nullable=False)
    bbox_x2: Mapped[float] = mapped_column(nullable=False)
    bbox_y2: Mapped[float] = mapped_column(nullable=False)
    qdrant_point_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vector_status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'PENDING'"))
    embedding_type: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'BODY'"))
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    image: Mapped[ImageRecord] = relationship(back_populates="instances")


class IngestRequestRecord(Base):
    __tablename__ = "ingest_requests"
    __table_args__ = (
        CheckConstraint(
            "status in ('RECEIVED', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ingest_requests_status_valid",
        ),
        Index("ingest_requests_scope_key_uq", "request_scope", "idempotency_key", unique=True),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_scope: Mapped[str] = mapped_column(String, nullable=False)
    image_id: Mapped[str | None] = mapped_column(
        ForeignKey("images.image_id", ondelete="SET NULL"),
        nullable=True,
    )
    request_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTER')",
            name="jobs_status_valid",
        ),
        Index("jobs_status_available_priority_idx", "status", "available_at", "priority", "created_at"),
        Index(
            "jobs_dedupe_key_uq",
            "job_type",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key is not null and status in ('QUEUED', 'LEASED', 'RUNNING')"
            ),
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["JobEventRecord"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobEventRecord(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("job_events_job_id_created_at_idx", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    job: Mapped[JobRecord] = relationship(back_populates="events")
