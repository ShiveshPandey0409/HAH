import logging

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, connection_url: str | None) -> None:
        self._pool = (
            AsyncConnectionPool(connection_url, open=False)
            if connection_url is not None
            else None
        )

    async def open(self) -> None:
        if self._pool is not None:
            await self._pool.open(wait=False)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def is_ready(self) -> bool:
        if self._pool is None:
            return False

        try:
            async with self._pool.connection(timeout=2) as connection:
                await connection.execute("SELECT 1")
        except Exception:
            logger.exception("Database readiness check failed")
            return False

        return True
