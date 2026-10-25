"""Database engine, sessions and the SQLite connection pragmas.

The engine is built lazily on first access from :func:`app.config.resolve_paths`,
so importing this module never touches the filesystem.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.config import resolve_paths
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def apply_pragmas(connection: Any, record: Any) -> None:
    """Apply the required SQLite pragmas to every new DBAPI connection."""
    cursor = connection.cursor()
    try:
        for statement in SQLITE_PRAGMAS:
            cursor.execute(statement)
    finally:
        cursor.close()


def reset_engine() -> None:
    """Dispose the cached engine so the next access rebuilds it (used by tests)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def _build_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", apply_pragmas)
    return engine


def _get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        paths = resolve_paths()
        paths.db.parent.mkdir(parents=True, exist_ok=True)
        _engine = _build_engine(paths.db)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _get_engine()
    assert _session_factory is not None
    return _session_factory


def new_session() -> Session:
    """Create a new Session bound to the application engine."""
    return _get_session_factory()()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yield one Session and always close it afterwards."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()


def __getattr__(name: str) -> Any:
    """Expose ``engine`` and ``SessionLocal`` as lazily built module attributes."""
    if name == "engine":
        return _get_engine()
    if name == "SessionLocal":
        return _get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
