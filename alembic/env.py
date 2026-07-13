# Ruta: alembic/env.py
"""
Alembic environment configured for async (asyncpg) migrations.

Key responsibilities:
  1. Pull the DB URL from `src.config.Settings` (single source of truth) —
     using the async DSN so migrations run over asyncpg.
  2. Import EVERY module's models so `Base.metadata` is fully populated
     before autogenerate diffs it against the live database. Missing an
     import here would make Alembic think those tables should be dropped.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.config import get_settings
from src.database import Base

# Import all models so their tables register on Base.metadata. These imports
# look unused, but they are load-bearing — do not remove.
import src.users.models  # noqa: F401,E402
import src.rooms.models  # noqa: F401,E402
import src.documents.models  # noqa: F401,E402
import src.rag.models  # noqa: F401,E402
import src.generation.models  # noqa: F401,E402

# Alembic Config object (values from alembic.ini).
config = context.config

# Inject the async DB URL from application settings at runtime.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.async_db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes (e.g. VARCHAR length) in autogenerate.
        compare_type=True,
        # Detect server_default changes too.
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`--sql` mode)."""
    context.configure(
        url=settings.async_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure_context(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database over an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())