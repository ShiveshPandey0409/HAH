from __future__ import annotations

from alembic.config import Config

from app.db.alembic_config import escape_alembic_config_value


def test_percent_encoded_database_url_survives_config_parser() -> None:
    database_url = "postgresql+asyncpg://user:p%40ss@localhost/hire_human"
    config = Config()

    config.set_main_option("sqlalchemy.url", escape_alembic_config_value(database_url))

    assert config.get_main_option("sqlalchemy.url") == database_url
