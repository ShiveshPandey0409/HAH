from __future__ import annotations

import asyncio
import logging

from app.db.session import AsyncSessionFactory
from app.services.payments import PaymentRuntime, process_next_payment

logger = logging.getLogger(__name__)


async def run_payment_worker(
    *,
    runtime: PaymentRuntime,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            processed = await process_next_payment(
                AsyncSessionFactory,
                runtime=runtime,
            )
        except Exception:
            logger.error("Payment worker iteration failed")
            processed = False
        if processed:
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=runtime.policy.poll_interval_seconds,
            )
        except TimeoutError:
            pass
