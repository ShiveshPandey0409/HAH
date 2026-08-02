from __future__ import annotations

from sqlalchemy.engine import make_url


def normalize_async_database_url(value: str) -> str:
    """Return a PostgreSQL URL that explicitly selects SQLAlchemy's asyncpg driver."""

    url = make_url(value)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use PostgreSQL with the asyncpg driver")
    return url.render_as_string(hide_password=False)


def asyncpg_database_url(value: str) -> str:
    """Return the driver-neutral PostgreSQL URL accepted by asyncpg."""

    url = make_url(normalize_async_database_url(value)).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)
