"""Alembic environment for paperbox.

The DSN always comes from ``app.core.config.settings`` (i.e. the root ``.env``),
so no credentials live in ``alembic.ini`` or in version control. The psycopg 3
driver string (``postgresql+psycopg://``) is passed through untouched.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Read the DSN from the environment/.env, never from alembic.ini."""
    dsn = config.get_main_option("sqlalchemy.url") or settings.database_url
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is not configured")
    # ConfigParser treats '%' as interpolation syntax; escape it for safety.
    return dsn.replace("%", "%%")


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Skip tables that are not owned by this application."""
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of touching a database."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    from sqlalchemy import create_engine

    connectable = create_engine(get_url(), pool_pre_ping=True, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
