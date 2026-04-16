"""initial reid queue schema

Revision ID: 20260416_0001
Revises:
Create Date: 2026-04-16 19:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260416_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("image_id", sa.String(), nullable=False),
        sa.Column("image_role", sa.String(), nullable=False),
        sa.Column("daycare_id", sa.String(), nullable=True),
        sa.Column("trainer_id", sa.String(), nullable=True),
        sa.Column("input_pet_name", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("raw_path", sa.Text(), nullable=False),
        sa.Column("thumb_path", sa.Text(), nullable=False),
        sa.Column("storage_state", sa.String(), server_default=sa.text("'READY'"), nullable=False),
        sa.Column("pipeline_version", sa.String(), nullable=True),
        sa.Column("source_detection_count", sa.Integer(), nullable=True),
        sa.Column("primary_source_detection_index", sa.Integer(), nullable=True),
        sa.Column("ingest_status", sa.String(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("pipeline_stage", sa.String(), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("image_role in ('DAILY', 'SEED')", name="images_image_role_valid"),
        sa.CheckConstraint(
            "storage_state in ('PENDING', 'READY', 'MISSING', 'DELETED')",
            name="images_storage_state_valid",
        ),
        sa.CheckConstraint(
            "ingest_status in ('PENDING', 'PROCESSING', 'READY', 'FAILED')",
            name="images_ingest_status_valid",
        ),
        sa.CheckConstraint(
            "pipeline_stage is null or pipeline_stage in ('RECEIVED', 'STORED', 'WAITING_FOR_SCHEDULER', "
            "'DETECTING', 'CROPPING', 'EMBEDDING', 'UPSERTING_VECTOR', 'FINALIZING', 'READY', 'FAILED', 'CANCELLED')",
            name="images_pipeline_stage_valid",
        ),
        sa.PrimaryKeyConstraint("image_id", name=op.f("pk_images")),
    )
    op.create_index("images_business_date_idx", "images", ["business_date"], unique=False)
    op.create_index("images_daycare_date_idx", "images", ["daycare_id", "business_date"], unique=False)
    op.create_index("images_ingest_status_idx", "images", ["ingest_status", "business_date"], unique=False)
    op.create_index("images_role_date_idx", "images", ["image_role", "business_date"], unique=False)

    op.create_table(
        "instances",
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("image_id", sa.String(), nullable=False),
        sa.Column("species", sa.String(), nullable=False),
        sa.Column("class_id", sa.Integer(), nullable=False),
        sa.Column("det_conf", sa.Float(), nullable=False),
        sa.Column("bbox_x1", sa.Float(), nullable=False),
        sa.Column("bbox_y1", sa.Float(), nullable=False),
        sa.Column("bbox_x2", sa.Float(), nullable=False),
        sa.Column("bbox_y2", sa.Float(), nullable=False),
        sa.Column("qdrant_point_id", sa.String(), nullable=True),
        sa.Column("vector_status", sa.String(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("embedding_type", sa.String(), server_default=sa.text("'BODY'"), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "vector_status in ('PENDING', 'READY', 'FAILED', 'DELETED')",
            name="instances_vector_status_valid",
        ),
        sa.ForeignKeyConstraint(["image_id"], ["images.image_id"], name=op.f("fk_instances_image_id_images"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("instance_id", name=op.f("pk_instances")),
    )
    op.create_index("instances_image_id_idx", "instances", ["image_id"], unique=False)
    op.create_index("instances_qdrant_point_id_uq", "instances", ["qdrant_point_id"], unique=True, postgresql_where=sa.text("qdrant_point_id is not null"))
    op.create_index("instances_species_idx", "instances", ["species"], unique=False)
    op.create_index("instances_vector_status_idx", "instances", ["vector_status"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTER')",
            name="jobs_status_valid",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "jobs_dedupe_key_uq",
        "jobs",
        ["job_type", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key is not null and status in ('QUEUED', 'LEASED', 'RUNNING')"),
    )
    op.create_index(
        "jobs_status_available_priority_idx",
        "jobs",
        ["status", "available_at", "priority", "created_at"],
        unique=False,
    )

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], name=op.f("fk_job_events_job_id_jobs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_events")),
    )
    op.create_index("job_events_job_id_created_at_idx", "job_events", ["job_id", "created_at"], unique=False)

    op.create_table(
        "ingest_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_scope", sa.String(), nullable=False),
        sa.Column("image_id", sa.String(), nullable=True),
        sa.Column("request_hash", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('RECEIVED', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ingest_requests_status_valid",
        ),
        sa.ForeignKeyConstraint(["image_id"], ["images.image_id"], name=op.f("fk_ingest_requests_image_id_images"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("request_id", name=op.f("pk_ingest_requests")),
    )
    op.create_index("ingest_requests_scope_key_uq", "ingest_requests", ["request_scope", "idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ingest_requests_scope_key_uq", table_name="ingest_requests")
    op.drop_table("ingest_requests")
    op.drop_index("job_events_job_id_created_at_idx", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("jobs_status_available_priority_idx", table_name="jobs")
    op.drop_index("jobs_dedupe_key_uq", table_name="jobs", postgresql_where=sa.text("dedupe_key is not null and status in ('QUEUED', 'LEASED', 'RUNNING')"))
    op.drop_table("jobs")
    op.drop_index("instances_vector_status_idx", table_name="instances")
    op.drop_index("instances_species_idx", table_name="instances")
    op.drop_index("instances_qdrant_point_id_uq", table_name="instances", postgresql_where=sa.text("qdrant_point_id is not null"))
    op.drop_index("instances_image_id_idx", table_name="instances")
    op.drop_table("instances")
    op.drop_index("images_role_date_idx", table_name="images")
    op.drop_index("images_ingest_status_idx", table_name="images")
    op.drop_index("images_daycare_date_idx", table_name="images")
    op.drop_index("images_business_date_idx", table_name="images")
    op.drop_table("images")
