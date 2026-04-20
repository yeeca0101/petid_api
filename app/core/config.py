from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables.

    PoC defaults are chosen to make it easy to run on a single GPU server.
    """

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # API
    app_name: str = "dogface-embedding-api"
    api_prefix: str = "/v1"
    log_level: str = "INFO"
    log_tz: str = Field(
        default="Asia/Seoul",
        description="Timezone label for app log formatter. Example: UTC, Asia/Seoul, KST",
    )
    log_tz_offset: Optional[str] = Field(
        default=None,
        description="Explicit log timezone offset. Example: +09:00, -05:30",
    )
    business_tz: str = Field(
        default="Asia/Seoul", # Asia/Seoul
        description="Business timezone for date-based filters and naive captured_at parsing.",
    )

    # Storage
    storage_dir: Path = Field(
        default=Path("./data"),
        description="Legacy base storage directory.",
    )
    reid_storage_dir: Path = Field(
        default=Path("./data/reid"),
        description="Storage root for Re-ID APIs (ingest/images/classify/exemplars).",
    )
    verification_storage_dir: Path = Field(
        default=Path("./data/verification"),
        description="Storage root for verification APIs (sync-images/trials).",
    )
    shared_storage_dir: Path = Field(
        default=Path("./data/shared"),
        description="Storage root for shared resources (registry/migrations).",
    )
    thumbnail_max_side_px: int = Field(
        default=512,
        ge=64,
        le=4096,
        description="Max side length (px) for generated thumbnails.",
    )

    # Model
    model_name: str = Field(
        default="miewid",
        description="Embedding model selector. Supported: miewid, mega-l-224, mega-t, clip, dinov2",
    )
    verification_model_name: str = Field(
        default="miewid",
        description="Embedding model selector for verification APIs.",
    )
    verification_miewid_model_source: str = Field(
        default="conservationxlabs/miewid-msv3",
        description="HF/local model source for verification miewid load. Relative local paths are resolved against HF_CACHE_DIR.",
    )
    verification_miewid_finetune_ckpt_path: Optional[Path] = Field(
        default=None,
        description="Optional verification miewid finetune checkpoint path.",
    )
    verification_weight_mode: Literal["auto", "hf", "ft"] = Field(
        default="auto",
        description="Weight selection mode for verification embedder. auto=legacy(hf then optional ft), hf=HF only, ft=FT only.",
    )
    reid_model_name: str = Field(
        default="miewid",
        description="Embedding model selector for Re-ID ingest/classification.",
    )
    reid_miewid_model_source: str = Field(
        default="conservationxlabs/miewid-msv3",
        description="HF/local model source for Re-ID miewid load. Relative local paths are resolved against HF_CACHE_DIR.",
    )
    reid_miewid_finetune_ckpt_path: Optional[Path] = Field(
        default=None,
        description="Optional Re-ID miewid finetune checkpoint path.",
    )
    reid_weight_mode: Literal["auto", "hf", "ft"] = Field(
        default="auto",
        description="Weight selection mode for re-id embedder. auto=legacy(hf then optional ft), hf=HF only, ft=FT only.",
    )
    hf_cache_dir: Path = Field(
        default=Path("./weights"),
        description="HuggingFace/TIMM cache directory (mounted volume recommended on server).",
    )

    # Detector (YOLO)
    detector_enabled: bool = Field(
        default=True,
        description="Enable YOLO detector to extract per-instance crops.",
    )
    yolo_weights_path: Path = Field(
        default=Path("./weights/yolo/yolo26x-seg.pt"),
        description="Path to YOLO26x weights file (.pt).",
    )
    yolo_task: Literal["detect", "segment"] = Field(
        default="segment",
        description="YOLO task. Use 'segment' when using a -seg weights file.",
    )
    yolo_imgsz: int = Field(
        default=640,
        ge=112,
        le=2048,
        description="YOLO inference image size.",
    )
    yolo_conf: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="YOLO confidence threshold.",
    )
    yolo_iou: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="YOLO NMS IoU threshold.",
    )
    yolo_class_ids: str = Field(
        default="15,16",
        description="Comma-separated class IDs to keep (Cat=15, Dog=16 in your plan).",
    )
    crop_padding: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="Padding ratio added around YOLO bbox when cropping (helps include body).",
    )
    min_bbox_area_ratio: float = Field(
        default=0.008,
        ge=0.0,
        le=1.0,
        description="Minimum normalized bbox area ratio required to keep a detection during ingest.",
    )
    apply_min_bbox_area_to_seed: bool = Field(
        default=True,
        description="Apply minimum bbox area filtering to SEED ingest detections.",
    )
    apply_min_bbox_area_to_daily: bool = Field(
        default=True,
        description="Apply minimum bbox area filtering to DAILY ingest detections.",
    )

    # Vector DB (Qdrant)
    vector_db: Literal["qdrant"] = Field(
        default="qdrant",
        description="Vector DB backend selector for this PoC.",
    )
    qdrant_url: str = Field(
        default="http://qdrant:6333",
        description="Qdrant base URL.",
    )
    qdrant_api_key: Optional[str] = Field(
        default=None,
        description="Qdrant API key (optional; for managed deployments).",
    )
    qdrant_collection: str = Field(
        default="pet_instances_v1",
        description="Qdrant collection name for instance embeddings.",
    )
    qdrant_timeout_s: float = Field(
        default=5.0,
        ge=0.5,
        le=60.0,
        description="Qdrant client timeout seconds.",
    )

    # PostgreSQL / queue
    postgres_host: str = Field(
        default="localhost",
        description="PostgreSQL host name.",
    )
    postgres_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="PostgreSQL port.",
    )
    postgres_db: str = Field(
        default="petid",
        description="PostgreSQL database name.",
    )
    postgres_user: str = Field(
        default="petid",
        description="PostgreSQL user name.",
    )
    postgres_password: str = Field(
        default="petid_dev_password",
        description="PostgreSQL password.",
    )
    database_url: Optional[str] = Field(
        default=None,
        description="Full SQLAlchemy database URL. Overrides host/user/password fields when set.",
    )
    db_pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Default DB connection pool size.",
    )
    db_max_overflow: int = Field(
        default=20,
        ge=0,
        le=200,
        description="DB connection pool overflow size.",
    )
    db_pool_timeout_s: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        description="Seconds to wait for a DB connection from the pool.",
    )
    read_source_default: Literal["filesystem", "postgres"] = Field(
        default="filesystem",
        description="Default metadata read source during migration.",
    )
    enable_dual_write: bool = Field(
        default=False,
        description="Enable dual-write from legacy stores into PostgreSQL during migration.",
    )
    enable_postgres_queue: bool = Field(
        default=False,
        description="Enable PostgreSQL-backed job queue paths.",
    )
    ingest_pipeline_slots: int = Field(
        default=1,
        ge=1,
        le=32,
        validation_alias=AliasChoices("INGEST_PIPELINE_SLOTS", "INGEST_PIPELINE_THREADS"),
        description=(
            "Planned concurrency knob for ingest pipeline replica slots. "
            "Current code path still runs effectively single-slot."
        ),
    )
    ingest_pipeline_local_queue_capacity: int = Field(
        default=2,
        ge=0,
        le=10000,
        description=(
            "Planned bounded local dispatch queue size for slot-based ingest execution. "
            "Not active in the current single-slot worker path."
        ),
    )
    ingest_pipeline_recommend_safety_vram_gb: float = Field(
        default=3.0,
        ge=0.0,
        le=256.0,
        description="Planned safety margin for VRAM-based ingest slot recommendation tooling.",
    )
    ingest_pipeline_recommend_safety_ram_gb: float = Field(
        default=4.0,
        ge=0.0,
        le=1024.0,
        description="Planned safety margin for system RAM-based ingest slot recommendation tooling.",
    )
    ingest_pipeline_probe_image: Optional[Path] = Field(
        default=None,
        description="Optional probe image path for future ingest slot sizing/profiling tooling.",
    )
    queue_local_capacity: int = Field(
        default=8,
        ge=1,
        le=10000,
        description="Max in-process jobs buffered per scheduler lane.",
    )
    queue_poll_interval_ms: int = Field(
        default=1000,
        ge=50,
        le=60000,
        description="Worker poll interval for queued jobs in milliseconds.",
    )
    queue_lease_timeout_s: int = Field(
        default=120,
        ge=5,
        le=86400,
        description="Seconds before a leased/running job is considered stale.",
    )
    queue_max_retries_default: int = Field(
        default=3,
        ge=0,
        le=100,
        description="Default max retries for new queue jobs.",
    )
    scheduler_lane_count: int = Field(
        default=1,
        ge=1,
        le=32,
        description="Number of logical in-process scheduler lanes.",
    )
    scheduler_max_inflight_jobs: int = Field(
        default=1,
        ge=1,
        le=32,
        description="Max concurrently executing jobs per lane.",
    )
    scheduler_enable_micro_batching: bool = Field(
        default=False,
        description="Enable cross-job micro-batching in the scheduler.",
    )
    scheduler_max_batch_items: int = Field(
        default=16,
        ge=1,
        le=512,
        description="Max items in one scheduler micro-batch.",
    )
    scheduler_max_batch_wait_ms: int = Field(
        default=25,
        ge=0,
        le=5000,
        description="Max wait time to accumulate a micro-batch in milliseconds.",
    )

    # Compute
    device: str = Field(
        default="cuda:0",
        description="Torch device string. Example: cuda:0, cuda:1, cpu",
    )
    max_concurrency: int = Field(
        default=1,
        ge=1,
        le=8,
        description="Max concurrent GPU inference requests allowed (protects OOM).",
    )
    max_batch_size: int = Field(
        default=16,
        ge=1,
        le=128,
        description="Max number of images per batch request.",
    )

    # Preprocess
    input_size: Optional[int] = Field(
        default=None,
        description="Override input resize size (square). If None, use model default.",
    )

    # Output
    output_dtype: Literal["float32", "float16"] = Field(
        default="float32",
        description="Embedding dtype in responses (binary endpoints).",
    )
    response_format: Literal["json", "f32", "f16"] = Field(
        default="json",
        description="Default response format for /embed endpoints.",
    )

    # Security/limits
    max_image_bytes: int = Field(
        default=15 * 1024 * 1024,
        description="Max upload image size in bytes (protects server).",
    )


settings = Settings()
