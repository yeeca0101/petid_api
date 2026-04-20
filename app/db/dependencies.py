from __future__ import annotations

from collections.abc import Generator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import DatabaseManager


def get_db_manager(request: Request) -> DatabaseManager:
    manager = getattr(request.app.state, "db", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Database manager not ready")
    return manager


def get_db_session(request: Request) -> Generator[Session, None, None]:
    manager = get_db_manager(request)
    with manager.session_scope() as session:
        yield session
