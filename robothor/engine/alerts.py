"""Centralized alert utility — replaces scattered Telegram alert sends.

Provides a single ``alert()`` function that dispatches alerts to configured
channels (Telegram, webhook, etc.). This replaces the hardcoded Telegram
sends in daemon.py's watchdog health checks.

Usage::

    from robothor.engine.alerts import alert

    await alert("critical", "PostgreSQL down", "3 consecutive ping failures", channel="telegram")
"""

from __future__ import annotations

import html
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def alert(
    level: str,
    title: str,
    body: str,
    *,
    channel: str = "telegram",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Send an alert to the configured channel.

    Args:
        level: Alert severity — "info", "warning", "critical".
        title: Short alert title (one line).
        body: Alert details (can be multiline).
        channel: Delivery channel — "telegram" (default), "webhook".
        metadata: Optional structured data for the alert.

    Returns:
        True if alert was delivered successfully.
    """
    if channel == "telegram":
        return await _send_telegram(level, title, body)
    elif channel == "webhook":
        return await _send_webhook(level, title, body, metadata)
    else:
        logger.warning("Unknown alert channel: %s", channel)
        return False


async def _send_telegram(level: str, title: str, body: str) -> bool:
    """Send alert via Telegram bot."""
    try:
        from robothor.engine.delivery import get_telegram_sender

        send_fn = get_telegram_sender()
        if send_fn is None:
            logger.warning("Telegram sender not initialized, can't deliver alert")
            return False
        icon = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "critical": "\U0001f6a8"}.get(
            level, "\u2753"
        )
        message = f"{icon} <b>{html.escape(title)}</b>\n{html.escape(body)}"
        await send_fn(message)
        return True
    except Exception as e:
        logger.warning("Alert delivery to Telegram failed: %s", e)
        return False


async def _send_webhook(level: str, title: str, body: str, metadata: dict[str, Any] | None) -> bool:
    """Send alert via webhook (extensibility point for PagerDuty, Slack, etc.)."""
    import os

    webhook_url = os.environ.get("ROBOTHOR_ALERT_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("No ROBOTHOR_ALERT_WEBHOOK_URL configured, skipping webhook alert")
        return False

    try:
        import httpx

        payload = {"level": level, "title": title, "body": body, "metadata": metadata or {}}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            return resp.status_code < 400
    except Exception as e:
        logger.warning("Alert delivery to webhook failed: %s", e)
        return False
