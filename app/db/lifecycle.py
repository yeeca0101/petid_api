from __future__ import annotations

from fastapi import FastAPI

from app.core.config import settings
from app.db.session import DatabaseManager


def init_db_state(app: FastAPI) -> DatabaseManager:
    """Attach a lazy DB manager to app state without forcing a connection."""
    manager = DatabaseManager(settings)
    app.state.db = manager
    return manager


def dispose_db_state(app: FastAPI) -> None:
    manager = getattr(app.state, "db", None)
    if manager is not None:
        manager.dispose()
