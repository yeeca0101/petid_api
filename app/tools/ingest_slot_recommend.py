from __future__ import annotations

import argparse
import math
import os
import threading
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BYTES_PER_GIB = 1024**3


@dataclass(frozen=True)
class CapacitySnapshot:
    total_ram_gb: float | None
    total_vram_gb: float | None
    gpu_name: str | None


@dataclass(frozen=True)
class SlotRecommendation:
    recommended_slots: int
    ram_limited_slots: int | None
    vram_limited_slots: int | None
    usable_ram_gb: float | None
    usable_vram_gb: float | None


@dataclass(frozen=True)
class BatchPipelineRecommendation:
    mode: str
    job_batch_size: int
    detector_batch_size: int
    embedder_crop_batch_size: int
    effective_images_in_gpu_path: int
    estimated_crops_per_job_batch: int | None


@dataclass(frozen=True)
class RecommendationConfig:
    safety_vram_gb: float
    safety_ram_gb: float
    probe_image: Path | None


@dataclass(frozen=True)
class RuntimeProbeUsage:
    measured_per_slot_ram_gb: float
    measured_per_slot_vram_gb: float | None
    build_peak_ram_delta_gb: float
    build_peak_vram_delta_gb: float | None
    probe_peak_ram_delta_gb: float
    probe_peak_vram_delta_gb: float | None
    detected_instances: int
    cropped_instances: int
    embedded_instances: int
    embedding_dim: int | None
    model_version: str
    image_width: int
    image_height: int
    image_role: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recommend a conservative INGEST_PIPELINE_SLOTS value for the current machine.",
        epilog=(
            "Examples:\n"
            "  python3 -m app.tools.ingest_slot_recommend\n"
            "  python3 -m app.tools.ingest_slot_recommend --probe-runtime\n"
            "  python3 -m app.tools.ingest_slot_recommend --probe-runtime --image-role SEED\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--per-slot-vram-gb",
        type=float,
        default=6.0,
        help="Conservative VRAM cost estimate per ingest slot. Default: 6.0",
    )
    parser.add_argument(
        "--per-slot-ram-gb",
        type=float,
        default=8.0,
        help="Conservative system RAM cost estimate per ingest slot. Default: 8.0",
    )
    parser.add_argument(
        "--min-slots",
        type=int,
        default=1,
        help="Minimum recommendation floor. Default: 1",
    )
    parser.add_argument(
        "--max-slots",
        type=int,
        default=32,
        help="Maximum recommendation cap. Default: 32",
    )
    parser.add_argument(
        "--probe-runtime",
        action="store_true",
        help="Run a real detector/embedder probe using INGEST_PIPELINE_PROBE_IMAGE.",
    )
    parser.add_argument(
        "--image-role",
        default="DAILY",
        choices=("DAILY", "SEED"),
        help="Image role used for runtime probe behavior. Default: DAILY",
    )
    parser.add_argument(
        "--job-batch-size",
        type=int,
        default=8,
        help="Candidate INGEST_JOB_BATCH_SIZE to include in batch recommendations. Default: 8",
    )
    parser.add_argument(
        "--detector-batch-size",
        type=int,
        default=8,
        help="Candidate DETECTOR_BATCH_SIZE to include in batch recommendations. Default: 8",
    )
    parser.add_argument(
        "--embedder-crop-batch-size",
        type=int,
        default=32,
        help="Candidate EMBEDDER_CROP_BATCH_SIZE to include in batch recommendations. Default: 32",
    )
    return parser


def _bytes_to_gib(value: int) -> float:
    return value / _BYTES_PER_GIB


def _read_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return float(raw_value)


def read_recommendation_config() -> RecommendationConfig:
    probe_image_raw = os.environ.get("INGEST_PIPELINE_PROBE_IMAGE", "").strip()
    return RecommendationConfig(
        safety_vram_gb=_read_float_env("INGEST_PIPELINE_RECOMMEND_SAFETY_VRAM_GB", 3.0),
        safety_ram_gb=_read_float_env("INGEST_PIPELINE_RECOMMEND_SAFETY_RAM_GB", 4.0),
        probe_image=Path(probe_image_raw) if probe_image_raw else None,
    )


def _read_total_ram_gb() -> float | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        if not line.startswith("MemTotal:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        return int(parts[1]) / (1024**2)
    return None


def _read_total_vram_gb() -> tuple[float | None, str | None]:
    if shutil.which("nvidia-smi") is None:
        return None, None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None, None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None, None

    gpu_name, memory_total_mb = [part.strip() for part in lines[0].split(",", maxsplit=1)]
    return float(memory_total_mb) / 1024.0, gpu_name


def read_capacity_snapshot() -> CapacitySnapshot:
    total_vram_gb, gpu_name = _read_total_vram_gb()
    return CapacitySnapshot(
        total_ram_gb=_read_total_ram_gb(),
        total_vram_gb=total_vram_gb,
        gpu_name=gpu_name,
    )


def _bounded_floor(value: float, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, math.floor(value)))


def recommend_slots(
    *,
    capacity: CapacitySnapshot,
    per_slot_vram_gb: float,
    per_slot_ram_gb: float,
    safety_vram_gb: float,
    safety_ram_gb: float,
    minimum: int,
    maximum: int,
) -> SlotRecommendation:
    ram_limited_slots = None
    usable_ram_gb = None
    if capacity.total_ram_gb is not None and per_slot_ram_gb > 0:
        usable_ram_gb = max(0.0, capacity.total_ram_gb - safety_ram_gb)
        ram_limited_slots = _bounded_floor(usable_ram_gb / per_slot_ram_gb, minimum=minimum, maximum=maximum)

    vram_limited_slots = None
    usable_vram_gb = None
    if capacity.total_vram_gb is not None and per_slot_vram_gb > 0:
        usable_vram_gb = max(0.0, capacity.total_vram_gb - safety_vram_gb)
        vram_limited_slots = _bounded_floor(usable_vram_gb / per_slot_vram_gb, minimum=minimum, maximum=maximum)

    candidates = [limit for limit in (ram_limited_slots, vram_limited_slots) if limit is not None]
    recommended_slots = min(candidates) if candidates else minimum
    recommended_slots = max(minimum, min(maximum, recommended_slots))
    return SlotRecommendation(
        recommended_slots=recommended_slots,
        ram_limited_slots=ram_limited_slots,
        vram_limited_slots=vram_limited_slots,
        usable_ram_gb=usable_ram_gb,
        usable_vram_gb=usable_vram_gb,
    )


def recommend_batch_pipeline(
    *,
    recommended_slots: int,
    job_batch_size: int,
    detector_batch_size: int,
    embedder_crop_batch_size: int,
    runtime_probe: RuntimeProbeUsage | None,
) -> BatchPipelineRecommendation:
    safe_job_batch_size = max(1, int(job_batch_size))
    safe_detector_batch_size = max(1, min(int(detector_batch_size), safe_job_batch_size))
    safe_embedder_crop_batch_size = max(1, int(embedder_crop_batch_size))
    estimated_crops_per_job_batch = None
    if runtime_probe is not None:
        estimated_crops_per_job_batch = safe_job_batch_size * max(0, int(runtime_probe.cropped_instances))
    return BatchPipelineRecommendation(
        mode="batch_full",
        job_batch_size=safe_job_batch_size,
        detector_batch_size=safe_detector_batch_size,
        embedder_crop_batch_size=safe_embedder_crop_batch_size,
        effective_images_in_gpu_path=max(1, int(recommended_slots)) * safe_job_batch_size,
        estimated_crops_per_job_batch=estimated_crops_per_job_batch,
    )


def _format_gb(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.1f} GiB"


def _read_current_rss_gb() -> float | None:
    status_file = Path("/proc/self/status")
    if not status_file.exists():
        return None
    for line in status_file.read_text(encoding="utf-8").splitlines():
        if not line.startswith("VmRSS:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        return int(parts[1]) / (1024**2)
    return None


class _PeakUsageMonitor:
    def __init__(self, *, torch_module=None, torch_device=None) -> None:
        self._torch = torch_module
        self._torch_device = torch_device
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline_rss_gb = _read_current_rss_gb() or 0.0
        self._peak_rss_gb = self._baseline_rss_gb
        self._baseline_vram_gb: float | None = None
        self._peak_vram_gb: float | None = None
        if self._torch is not None and self._torch_device is not None:
            self._baseline_vram_gb = self._read_current_vram_gb()
            self._peak_vram_gb = self._baseline_vram_gb

    def _read_current_vram_gb(self) -> float | None:
        if self._torch is None or self._torch_device is None:
            return None
        try:
            return self._torch.cuda.memory_reserved(self._torch_device) / _BYTES_PER_GIB
        except Exception:
            return None

    def _sample_once(self) -> None:
        current_rss_gb = _read_current_rss_gb()
        if current_rss_gb is not None:
            self._peak_rss_gb = max(self._peak_rss_gb, current_rss_gb)

        current_vram_gb = self._read_current_vram_gb()
        if current_vram_gb is not None and self._peak_vram_gb is not None:
            self._peak_vram_gb = max(self._peak_vram_gb, current_vram_gb)

    def _loop(self) -> None:
        while not self._stop_event.wait(0.02):
            self._sample_once()

    def __enter__(self) -> "_PeakUsageMonitor":
        self._thread = threading.Thread(target=self._loop, name="ingest-slot-probe-monitor", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._sample_once()

    @property
    def peak_rss_delta_gb(self) -> float:
        return max(0.0, self._peak_rss_gb - self._baseline_rss_gb)

    @property
    def peak_vram_delta_gb(self) -> float | None:
        if self._baseline_vram_gb is None or self._peak_vram_gb is None:
            return None
        return max(0.0, self._peak_vram_gb - self._baseline_vram_gb)


def run_runtime_probe(*, probe_image: Path, image_role: str) -> RuntimeProbeUsage:
    from app.core.config import settings as runtime_settings
    from app.worker.pipeline import _load_pil_image_from_path, build_probe_resources, run_ingest_probe

    pil_image = _load_pil_image_from_path(str(probe_image))

    torch_module = None
    torch_device = None
    try:
        import torch as torch_module  # type: ignore[no-redef]
    except Exception:
        torch_module = None

    with _PeakUsageMonitor() as build_monitor:
        resources = build_probe_resources(runtime_settings)

    if torch_module is not None and resources.embedder.device.type == "cuda":
        torch_device = resources.embedder.device
        try:
            torch_module.cuda.reset_peak_memory_stats(torch_device)
        except Exception:
            torch_device = None

    with _PeakUsageMonitor(torch_module=torch_module, torch_device=torch_device) as probe_monitor:
        probe_result = run_ingest_probe(resources=resources, pil_image=pil_image, image_role=image_role)
        if torch_module is not None and torch_device is not None:
            try:
                torch_module.cuda.synchronize(torch_device)
            except Exception:
                pass
        time.sleep(0.05)

    measured_per_slot_vram_gb = None
    if build_monitor.peak_vram_delta_gb is not None or probe_monitor.peak_vram_delta_gb is not None:
        measured_per_slot_vram_gb = (build_monitor.peak_vram_delta_gb or 0.0) + (probe_monitor.peak_vram_delta_gb or 0.0)

    return RuntimeProbeUsage(
        measured_per_slot_ram_gb=build_monitor.peak_rss_delta_gb + probe_monitor.peak_rss_delta_gb,
        measured_per_slot_vram_gb=measured_per_slot_vram_gb,
        build_peak_ram_delta_gb=build_monitor.peak_rss_delta_gb,
        build_peak_vram_delta_gb=build_monitor.peak_vram_delta_gb,
        probe_peak_ram_delta_gb=probe_monitor.peak_rss_delta_gb,
        probe_peak_vram_delta_gb=probe_monitor.peak_vram_delta_gb,
        detected_instances=int(probe_result["detected_instances"]),
        cropped_instances=int(probe_result["cropped_instances"]),
        embedded_instances=int(probe_result["embedded_instances"]),
        embedding_dim=int(probe_result["embedding_dim"]) if probe_result["embedding_dim"] is not None else None,
        model_version=str(probe_result["model_version"]),
        image_width=int(probe_result["image_size"][0]),
        image_height=int(probe_result["image_size"][1]),
        image_role=str(probe_result["image_role"]),
    )


def main() -> int:
    args = _build_parser().parse_args()
    config = read_recommendation_config()
    capacity = read_capacity_snapshot()
    runtime_probe: RuntimeProbeUsage | None = None
    per_slot_ram_gb = args.per_slot_ram_gb
    per_slot_vram_gb = args.per_slot_vram_gb

    if args.probe_runtime:
        if config.probe_image is None:
            raise SystemExit("INGEST_PIPELINE_PROBE_IMAGE must be set to use --probe-runtime.")
        if not config.probe_image.exists():
            raise SystemExit(f"Configured probe image does not exist: {config.probe_image}")
        runtime_probe = run_runtime_probe(probe_image=config.probe_image, image_role=args.image_role)
        per_slot_ram_gb = max(0.1, runtime_probe.measured_per_slot_ram_gb)
        if runtime_probe.measured_per_slot_vram_gb is not None:
            per_slot_vram_gb = max(0.1, runtime_probe.measured_per_slot_vram_gb)

    recommendation = recommend_slots(
        capacity=capacity,
        per_slot_vram_gb=per_slot_vram_gb,
        per_slot_ram_gb=per_slot_ram_gb,
        safety_vram_gb=config.safety_vram_gb,
        safety_ram_gb=config.safety_ram_gb,
        minimum=max(1, args.min_slots),
        maximum=max(1, args.max_slots),
    )
    batch_recommendation = recommend_batch_pipeline(
        recommended_slots=recommendation.recommended_slots,
        job_batch_size=args.job_batch_size,
        detector_batch_size=args.detector_batch_size,
        embedder_crop_batch_size=args.embedder_crop_batch_size,
        runtime_probe=runtime_probe,
    )

    print("Ingest Slot Recommendation")
    print(f"- detected_total_ram: {_format_gb(capacity.total_ram_gb)}")
    print(f"- detected_total_vram: {_format_gb(capacity.total_vram_gb)}")
    print(f"- detected_gpu: {capacity.gpu_name or 'unknown'}")
    print(f"- configured_safety_ram: {config.safety_ram_gb:.1f} GiB")
    print(f"- configured_safety_vram: {config.safety_vram_gb:.1f} GiB")
    if runtime_probe is None:
        print(f"- recommendation_mode: heuristic")
        print(f"- assumed_per_slot_ram: {per_slot_ram_gb:.1f} GiB")
        print(f"- assumed_per_slot_vram: {per_slot_vram_gb:.1f} GiB")
    else:
        print(f"- recommendation_mode: runtime_probe")
        print(f"- measured_per_slot_ram: {runtime_probe.measured_per_slot_ram_gb:.1f} GiB")
        print(f"- measured_per_slot_vram: {_format_gb(runtime_probe.measured_per_slot_vram_gb)}")
        print(f"- build_peak_ram_delta: {runtime_probe.build_peak_ram_delta_gb:.1f} GiB")
        print(f"- build_peak_vram_delta: {_format_gb(runtime_probe.build_peak_vram_delta_gb)}")
        print(f"- probe_peak_ram_delta: {runtime_probe.probe_peak_ram_delta_gb:.1f} GiB")
        print(f"- probe_peak_vram_delta: {_format_gb(runtime_probe.probe_peak_vram_delta_gb)}")
        print(f"- probe_image_size: {runtime_probe.image_width}x{runtime_probe.image_height}")
        print(f"- probe_image_role: {runtime_probe.image_role}")
        print(f"- probe_detected_instances: {runtime_probe.detected_instances}")
        print(f"- probe_cropped_instances: {runtime_probe.cropped_instances}")
        print(f"- probe_embedded_instances: {runtime_probe.embedded_instances}")
        print(f"- probe_embedding_dim: {runtime_probe.embedding_dim if runtime_probe.embedding_dim is not None else 'unknown'}")
        print(f"- probe_model_version: {runtime_probe.model_version}")
    print(f"- usable_ram_after_safety: {_format_gb(recommendation.usable_ram_gb)}")
    print(f"- usable_vram_after_safety: {_format_gb(recommendation.usable_vram_gb)}")
    print(f"- ram_limited_slots: {recommendation.ram_limited_slots if recommendation.ram_limited_slots is not None else 'unknown'}")
    print(f"- vram_limited_slots: {recommendation.vram_limited_slots if recommendation.vram_limited_slots is not None else 'unknown'}")
    print(f"- recommended_ingest_pipeline_slots: {recommendation.recommended_slots}")
    print(f"- recommended_ingest_batch_pipeline_mode: {batch_recommendation.mode}")
    print(f"- recommended_ingest_job_batch_size: {batch_recommendation.job_batch_size}")
    print(f"- recommended_detector_batch_size: {batch_recommendation.detector_batch_size}")
    print(f"- recommended_embedder_crop_batch_size: {batch_recommendation.embedder_crop_batch_size}")
    print(f"- recommended_effective_images_in_gpu_path: {batch_recommendation.effective_images_in_gpu_path}")
    print(
        "- estimated_crops_per_job_batch: "
        f"{batch_recommendation.estimated_crops_per_job_batch if batch_recommendation.estimated_crops_per_job_batch is not None else 'unknown'}"
    )
    print("Suggested env:")
    print(f"INGEST_PIPELINE_SLOTS={recommendation.recommended_slots}")
    print(f"INGEST_BATCH_PIPELINE_MODE={batch_recommendation.mode}")
    print(f"INGEST_JOB_BATCH_SIZE={batch_recommendation.job_batch_size}")
    print(f"DETECTOR_BATCH_SIZE={batch_recommendation.detector_batch_size}")
    print(f"EMBEDDER_CROP_BATCH_SIZE={batch_recommendation.embedder_crop_batch_size}")
    if config.probe_image is not None:
        print(f"- configured_probe_image: {config.probe_image}")
        print(
            f"- configured_probe_image_exists: {'yes' if config.probe_image.exists() else 'no'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
