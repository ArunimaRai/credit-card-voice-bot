"""POST outcome payloads to webhook.site (or whatever WEBHOOK_URL points at)."""

import os

import httpx
from loguru import logger


async def post_outcome(payload: dict) -> None:
    url = os.getenv("WEBHOOK_URL")
    if not url:
        logger.warning("WEBHOOK_URL not set — skipping webhook POST. Payload: {}", payload)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            logger.info(f"Webhook POST -> {resp.status_code}")
    except httpx.HTTPError as e:
        logger.error(f"Webhook POST failed: {e}")
