from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from qdrant_client import QdrantClient

from app.core.config import settings
from app.db.session import DatabaseManager


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset local runtime state for DB/storage.")
    parser.add_argument("--reset-db", action="store_true", help="Drop and recreate the public schema, then run migrations.")
    parser.add_argument("--reset-storage", action="store_true", help="Clear generated storage under reid/verification/shared.")
    return parser


def _storage_dirs() -> list[Path]:
    return [
        Path(settings.reid_storage_dir) / "meta",
        Path(settings.reid_storage_dir) / "images",
        Path(settings.reid_storage_dir) / "thumbs",
        Path(settings.reid_storage_dir) / "buckets",
        Path(settings.reid_storage_dir) / "exports",
        Path(settings.reid_storage_dir) / "registry",
    ]


def _clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _reset_storage() -> None:
    print("[RESET] clearing generated storage")
    for directory in _storage_dirs():
        _clear_directory(directory)
        print(f"[RESET] cleared {directory}")


def _reset_database() -> None:
    print("[RESET] dropping PostgreSQL public schema")
    manager = DatabaseManager(settings)
    engine = manager.get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")

    print("[RESET] running alembic upgrade head")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


def _wait_for_qdrant_client(timeout_s: int = 60) -> QdrantClient:
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.qdrant_timeout_s,
    )
    waited = 0
    step = 2
    last_exc: Exception | None = None
    while waited < timeout_s:
        try:
            client.get_collections()
            return client
        except Exception as exc:  # pragma: no cover - network timing
            last_exc = exc
            time.sleep(step)
            waited += step
    raise RuntimeError(f"Qdrant not reachable after {timeout_s}s: {last_exc}") from last_exc


def _reset_qdrant() -> None:
    print(f"[RESET] deleting Qdrant collection {settings.qdrant_collection}")
    client = _wait_for_qdrant_client()
    try:
        client.delete_collection(collection_name=settings.qdrant_collection)
    except Exception as exc:
        print(f"[RESET] qdrant collection delete failed (non-fatal): {exc}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    reset_db = args.reset_db or _env_flag("RESET_DB_ON_START")
    reset_storage = args.reset_storage or _env_flag("RESET_STORAGE_ON_START")

    if not reset_db and not reset_storage:
        print("[RESET] no reset flags set; skipping")
        return 0

    if reset_db:
        _reset_database()
        _reset_qdrant()

    if reset_storage:
        _reset_storage()

    print("[RESET] runtime state reset complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
