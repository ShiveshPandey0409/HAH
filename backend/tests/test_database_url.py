from __future__ import annotations

import pytest

from app.db.database_url import asyncpg_database_url, normalize_async_database_url


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_render_postgres_urls_select_asyncpg(scheme: str) -> None:
    value = f"{scheme}://user:p%40ss@db.internal/hah"

    assert normalize_async_database_url(value) == (
        "postgresql+asyncpg://user:p%40ss@db.internal/hah"
    )


def test_existing_asyncpg_url_is_preserved() -> None:
    value = "postgresql+asyncpg://user:password@localhost/hah"

    assert normalize_async_database_url(value) == value
    assert asyncpg_database_url(value) == "postgresql://user:password@localhost/hah"


def test_non_postgres_database_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        normalize_async_database_url("sqlite+aiosqlite:///hah.db")
