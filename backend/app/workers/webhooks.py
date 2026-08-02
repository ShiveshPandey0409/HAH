from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Sequence

from pydantic import SecretStr

from app.core.config import get_settings
from app.db.session import AsyncSessionFactory
from app.services.webhooks import (
    PinnedHTTPSWebhookTransport,
    SystemWebhookResolver,
    WebhookDeliveryPolicy,
    WebhookRuntime,
    build_webhook_cipher,
    process_next_webhook_delivery,
)

logger = logging.getLogger(__name__)


async def run_webhook_worker(
    *,
    runtime: WebhookRuntime,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            processed = await process_next_webhook_delivery(
                AsyncSessionFactory,
                runtime=runtime,
            )
        except Exception:
            logger.error("Webhook worker iteration failed")
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


def runtime_from_settings() -> WebhookRuntime:
    settings = get_settings()
    keys = getattr(settings, "webhook_secret_encryption_keys", None)
    if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)) or not keys:
        raise RuntimeError("WEBHOOK_SECRET_ENCRYPTION_KEYS must be configured")
    normalized_keys: list[str | bytes | SecretStr] = list(keys)
    policy = WebhookDeliveryPolicy(
        max_attempts=getattr(settings, "webhook_max_attempts", 6),
        timeout_seconds=getattr(settings, "webhook_timeout_seconds", 10.0),
        lease_seconds=getattr(settings, "webhook_lease_seconds", 30.0),
        backoff_base_seconds=getattr(settings, "webhook_retry_base_seconds", 30.0),
        backoff_cap_seconds=getattr(settings, "webhook_retry_cap_seconds", 3_600.0),
        poll_interval_seconds=getattr(settings, "webhook_poll_interval_seconds", 1.0),
        dns_timeout_seconds=getattr(settings, "webhook_dns_timeout_seconds", 3.0),
        response_body_limit_bytes=getattr(
            settings,
            "webhook_response_body_limit",
            4_096,
        ),
    )
    return WebhookRuntime(
        cipher=build_webhook_cipher(normalized_keys),
        resolver=SystemWebhookResolver(),
        transport=PinnedHTTPSWebhookTransport(),
        policy=policy,
    )


async def _run_from_settings() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    await run_webhook_worker(
        runtime=runtime_from_settings(),
        stop_event=stop_event,
    )


def main() -> None:
    asyncio.run(_run_from_settings())


if __name__ == "__main__":
    main()
