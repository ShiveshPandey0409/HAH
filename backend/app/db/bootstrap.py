from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
from alembic.config import Config

from alembic import command
from app.core.config import get_settings
from app.db.alembic_config import escape_alembic_config_value
from app.db.database_url import asyncpg_database_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCHEMA_PATH = BACKEND_DIR.parent / "database" / "schema.sql"
BASELINE_REVISION = "20260801_0001"
BASELINE_TABLES = frozenset(
    {
        "api_clients",
        "bounties",
        "bounty_claims",
        "mcp_requests",
        "payment_attempts",
        "payment_authorizations",
        "payments",
        "social_accounts",
        "submission_proofs",
        "submissions",
        "tasks",
        "users",
        "webhook_deliveries",
        "webhook_endpoints",
    }
)


async def ensure_sql_baseline(database_url: str) -> bool:
    """Create the SQL baseline when empty and report whether Alembic must stamp it."""

    connection = await asyncpg.connect(asyncpg_database_url(database_url))
    try:
        rows = await connection.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY($1::text[])
            """,
            sorted(BASELINE_TABLES),
        )
        present_tables = {row["table_name"] for row in rows}
        if present_tables and present_tables != BASELINE_TABLES:
            missing_tables = ", ".join(sorted(BASELINE_TABLES - present_tables))
            raise RuntimeError(f"database has a partial HAH baseline; missing: {missing_tables}")
        if not present_tables:
            await connection.execute(SCHEMA_PATH.read_text())

        version_table = await connection.fetchval("SELECT to_regclass('public.alembic_version')")
        if version_table is None:
            return True
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        return revision is None
    finally:
        await connection.close()


def migration_config(database_url: str) -> Config:
    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        escape_alembic_config_value(database_url),
    )
    return config


def main() -> None:
    database_url = get_settings().database_url
    should_stamp = asyncio.run(ensure_sql_baseline(database_url))
    config = migration_config(database_url)
    if should_stamp:
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
