"""Alembic environment. The schema is defined in app/db/schema.sql (single source of truth)."""
from __future__ import annotations

import os

from sqlalchemy import create_engine

from alembic import context
from app.config import normalize_database_url

config = context.config


def run_migrations_online() -> None:
    url = normalize_database_url(os.environ.get("DATABASE_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=normalize_database_url(os.environ.get("DATABASE_URL", "postgresql://")), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
