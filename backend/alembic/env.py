"""Alembic environment for the backend database."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.db.alembic_config import escape_alembic_config_value
from app.db.base import Base
from app.db.database_url import normalize_async_database_url
from app.models import (  # noqa: F401
    APIClient,
    Bounty,
    BountyClaim,
    MCPRequest,
    OAuthAuthorizationGrant,
    OAuthDelegation,
    OAuthIdentity,
    PasswordResetToken,
    SocialAccount,
    Submission,
    SubmissionProof,
    Task,
    User,
    UserSession,
    WebhookDelivery,
    WebhookEndpoint,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = normalize_async_database_url(
    os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
)
config.set_main_option("sqlalchemy.url", escape_alembic_config_value(database_url))
target_metadata = Base.metadata
managed_tables = frozenset(target_metadata.tables)


def include_object(
    _: object,
    name: str | None,
    type_: str,
    __: bool,
    ___: object | None,
) -> bool:
    return type_ != "table" or name in managed_tables


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
