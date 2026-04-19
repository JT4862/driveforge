"""Outbound webhook dispatch.

Fires a single JSON POST on batch completion. Payload shape is documented
in BUILD.md → Inventory & External Integration. This is the ONLY way
DriveForge pushes data off the box.

If the webhook URL is unset, dispatch is a no-op. Failures are logged,
retried with exponential backoff, then given up on — the test results are
already in the local DB either way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DEFAULT_ATTEMPTS = 5


async def dispatch(
    url: str | None,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
) -> bool:
    """Send the payload. Returns True on success, False on give-up.

    Designed to fail-closed: if the webhook is down, we log loudly and move
    on — never block the pipeline or retry forever.
    """
    if not url:
        return False
    backoff = 1.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(url, json=payload)
                if 200 <= resp.status_code < 300:
                    logger.info("webhook OK attempt=%d status=%d", attempt, resp.status_code)
                    return True
                logger.warning(
                    "webhook non-2xx attempt=%d status=%d body=%r",
                    attempt,
                    resp.status_code,
                    resp.text[:500],
                )
            except httpx.HTTPError as exc:
                logger.warning("webhook error attempt=%d err=%s", attempt, exc)
            if attempt < attempts:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    logger.error("webhook gave up after %d attempts url=%s", attempts, url)
    return False
