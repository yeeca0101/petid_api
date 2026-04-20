from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.reconciliation import build_reconciliation_report, report_to_dict
from app.db.session import DatabaseManager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Re-ID filesystem/PostgreSQL reconciliation report.",
    )
    parser.add_argument(
        "--format",
        choices=("pretty", "json"),
        default="pretty",
        help="Output format.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the JSON report to disk.",
    )
    parser.add_argument(
        "--fail-on-cutover-blockers",
        action="store_true",
        help="Exit non-zero when any cutover check is false.",
    )
    return parser


def _render_pretty(payload: dict) -> str:
    filesystem = payload["filesystem"]
    database = payload["database"]
    parity = payload["parity"]
    checks = payload["cutover_checks"]
    notes = payload["notes"]

    lines = [
        "Re-ID Reconciliation Report",
        f"generated_at: {payload['generated_at']}",
        "",
        "filesystem:",
        f"  meta_file_count: {filesystem['meta_file_count']}",
        f"  meta_instance_count: {filesystem['meta_instance_count']}",
        f"  pet_registry_exists: {filesystem['pet_registry_exists']}",
        f"  pet_registry_count: {filesystem['pet_registry_count']}",
        f"  missing_raw_path_count: {filesystem['missing_raw_path_count']}",
        f"  missing_thumb_path_count: {filesystem['missing_thumb_path_count']}",
        "",
        "database:",
        f"  image_count: {database['image_count']}",
        f"  instance_count: {database['instance_count']}",
        f"  images_ready_count: {database['images_ready_count']}",
        f"  images_failed_count: {database['images_failed_count']}",
        f"  instances_ready_count: {database['instances_ready_count']}",
        f"  instances_pending_count: {database['instances_pending_count']}",
        f"  jobs_queued_count: {database['jobs_queued_count']}",
        f"  jobs_leased_count: {database['jobs_leased_count']}",
        f"  jobs_running_count: {database['jobs_running_count']}",
        f"  jobs_failed_count: {database['jobs_failed_count']}",
        f"  stale_job_count: {database['stale_job_count']}",
        f"  queue_image_state_mismatch_count: {database['queue_image_state_mismatch_count']}",
        "",
        "parity:",
        f"  image_count_delta: {parity['image_count_delta']}",
        f"  instance_count_delta: {parity['instance_count_delta']}",
        "",
        "cutover_checks:",
    ]
    lines.extend(f"  {name}: {value}" for name, value in checks.items())
    if notes:
        lines.append("")
        lines.append("notes:")
        lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines)


def main() -> int:
    args = _build_parser().parse_args()
    setup_logging(settings.log_level)

    db = DatabaseManager(settings)
    report = build_reconciliation_report(db)
    payload = report_to_dict(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_pretty(payload))

    if args.fail_on_cutover_blockers and not all(payload["cutover_checks"].values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

