from __future__ import annotations


def escape_alembic_config_value(value: str) -> str:
    """Escape percent signs before storing a value in Alembic ConfigParser."""
    return value.replace("%", "%%")
