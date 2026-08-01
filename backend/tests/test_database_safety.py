from __future__ import annotations

import pytest

from tests.database_safety import UnsafeTestDatabaseError, require_safe_test_database_url


def test_accepts_explicit_test_database() -> None:
    database_url = "postgresql+asyncpg://postgres:postgres@localhost/hire_human_test"

    assert require_safe_test_database_url(database_url) == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://postgres:postgres@localhost/hire_human",
        "postgresql+asyncpg://postgres:postgres@localhost/hire_human_staging",
        "not a database url",
    ],
)
def test_rejects_non_test_database_before_connection(database_url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="Refusing|valid database URL"):
        require_safe_test_database_url(database_url)
