# Ruta: src/database.py
"""
Database engine, session factory and declarative base for XookHub.

This module centralizes all SQLAlchemy 2.0 async infrastructure used across
the modular monolith. Every domain module (users, rooms, documents, rag, ...)
imports `Base` from here to declare its ORM models, and `get_db` as a
FastAPI dependency to obtain a scoped AsyncSession.

Deliberately ONE engine, ONE dialect (asyncpg), used everywhere — FastAPI
via `get_db`, and the Celery worker via `AsyncSessionLocal` directly (see
`src/worker/tasks.py`, which bridges into it with `asyncio.run()`). An
earlier revision of this module also kept a second, psycopg2-backed *sync*
engine specifically for Celery, on the theory that a worker with no asyncio
event loop shouldn't need an async driver. That reasoning was sound, but it
left two DB configurations that could drift out of sync — and did: if
`DB_URL` ever resolved to an asyncpg DSN (e.g. copied from `async_db_url`),
the "sync" engine would silently end up asyncpg-backed too. asyncpg's
SQLAlchemy dialect always talks to the driver through a greenlet bridge
(`greenlet_spawn`), which only exists when you go through `AsyncSession`'s
async API — calling it from a plain sync `Session`, as Celery would, raised
`MissingGreenlet: greenlet_spawn has not been called`. Removed rather than
worked around: there is no longer a second engine configuration left to
accidentally misconfigure.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config import get_settings

# --------------------------------------------------------------------------- #
# Engine configuration
# --------------------------------------------------------------------------- #
settings = get_settings()

engine = create_async_engine(
    settings.async_db_url,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# --------------------------------------------------------------------------- #
# Declarative base
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    """Shared declarative base for every ORM model in the monolith."""

    pass


class CreatedAtMixin:
    """Adds a single DB-side `created_at` timestamp column.

    Only `users` needs both created_at/updated_at per the DDL; every other
    table in this schema only tracks created_at, so that lives here and
    `updated_at` is declared explicitly on the one model that needs it.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a transactional AsyncSession scoped to a single request.

    Commits on success, rolls back on any exception, and always closes the
    session. Routers/services should depend on this rather than importing
    AsyncSessionLocal directly, keeping persistence swappable (DIP).

    The Celery worker does NOT use this dependency (it's FastAPI-specific,
    tied to request scope) — it opens `AsyncSessionLocal()` directly inside
    an `async def` bridged via `asyncio.run()`. See `src/worker/tasks.py`.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()