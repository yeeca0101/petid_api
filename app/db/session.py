from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def build_database_url(settings: Settings) -> str:
    """Build a SQLAlchemy database URL from explicit settings when needed."""
    if settings.database_url:
        return settings.database_url
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    )


@dataclass
class DatabaseManager:
    """Lazy database bootstrap that keeps DB optional until enabled by later slices."""

    settings: Settings
    _database_url: str = field(init=False, repr=False)
    _engine: Optional[Engine] = field(default=None, init=False, repr=False)
    _session_factory: Optional[sessionmaker[Session]] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._database_url = build_database_url(self.settings)

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enable_dual_write or self.settings.enable_postgres_queue)

    def get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self._database_url,
                pool_pre_ping=True,
                pool_size=self.settings.db_pool_size,
                max_overflow=self.settings.db_max_overflow,
                pool_timeout=self.settings.db_pool_timeout_s,
            )
        return self._engine

    def get_session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.get_engine(),
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
                class_=Session,
            )
        return self._session_factory

    def session(self) -> Session:
        return self.get_session_factory()()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._session_factory = None
