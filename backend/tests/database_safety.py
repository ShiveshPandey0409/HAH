from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ALLOWED_TEST_DATABASE_NAMES = frozenset({"hire_human_test"})


class UnsafeTestDatabaseError(RuntimeError):
    pass


def require_safe_test_database_url(database_url: str) -> str:
    try:
        database_name = make_url(database_url).database
    except ArgumentError as error:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is not a valid database URL") from error

    if database_name not in ALLOWED_TEST_DATABASE_NAMES:
        allowed = ", ".join(sorted(ALLOWED_TEST_DATABASE_NAMES))
        raise UnsafeTestDatabaseError(
            f"Refusing destructive tests against database {database_name!r}; "
            f"allowed database names: {allowed}"
        )
    return database_url
