"""Centralized alert utility — replaces scattered Telegram alert sends.

Provides a single ``alert()`` function that dispatches alerts to configured
channels (Telegram, webhook, etc.). This replaces the hardcoded Telegram
sends in daemon.py's watchdog health checks.

Routing policy — page only what needs the operator:

- ``level='critical'`` pages Telegram immediately.
- ``level='warning'`` / ``'info'`` write an ``alert_digest`` row into
  ``crm_agent_notifications`` (to_agent='main') instead, so the morning
  briefing and heartbeat surface them without paging.

Delivery is verified, not assumed: the Telegram sender signals total
failure with an empty result list, and a failed page falls back to an
``alert_fallback`` notification row so the alert still reaches the next
briefing. ``alert()`` returns whether delivery actually happened.

Usage::

    from robothor.engine.alerts import alert

    await alert("critical", "PostgreSQL down", "3 consecutive ping failures", channel="telegram")
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Alert levels that page Telegram immediately. Everything else goes to the
# digest so the operator is only interrupted for things that need them.
_PAGE_LEVELS = frozenset({"critical"})


async def alert(
    level: str,
    title: str,
    body: str,
    *,
    channel: str = "telegram",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Send an alert, routed by severity.

    Args:
        level: Alert severity — "info", "warning", "critical". Only
            "critical" pages Telegram immediately; "warning"/"info" become
            an ``alert_digest`` notification row for the briefing/heartbeat.
        title: Short alert title (one line).
        body: Alert details (can be multiline).
        channel: Delivery channel — "telegram" (default), "webhook".
        metadata: Optional structured data for the alert.

    Returns:
        True if the alert was verifiably delivered (Telegram send returned
        sent messages, or the digest/fallback notification row was written).
    """
    if channel == "telegram":
        if level in _PAGE_LEVELS:
            return await _send_telegram(level, title, body)
        return await _write_notification("alert_digest", level, title, body, metadata)
    elif channel == "webhook":
        return await _send_webhook(level, title, body, metadata)
    else:
        logger.warning("Unknown alert channel: %s", channel)
        return False


async def _send_telegram(level: str, title: str, body: str) -> bool:
    """Page via Telegram; on failure, fall back to a notification row.

    ``TelegramBot.send_message`` swallows per-chunk exceptions and returns
    a list of sent messages — empty on total failure — so the result must
    be checked, not assumed (the arity bug hid behind exactly that
    assumption while 432+ alerts went nowhere).
    """
    delivered = False
    try:
        from robothor.engine.delivery import get_telegram_sender

        send_fn = get_telegram_sender()
        if send_fn is None:
            logger.warning("Telegram sender not initialized, can't deliver alert")
        else:
            # send_fn is TelegramBot.send_message(self, chat_id, text, **_ignored) —
            # alerts.py has no chat-id source of its own, so pull it from
            # EngineConfig (populated from ROBOTHOR_TELEGRAM_CHAT_ID / TELEGRAM_CHAT_ID).
            from robothor.engine.config import EngineConfig

            chat_id = EngineConfig.from_env().default_chat_id
            if not chat_id:
                logger.warning("No default_chat_id configured, can't deliver alert")
            else:
                icon = {
                    "info": "ℹ️",
                    "warning": "⚠️",
                    "critical": "\U0001f6a8",
                }.get(level, "❓")
                message = f"{icon} <b>{html.escape(title)}</b>\n{html.escape(body)}"
                sent = await send_fn(chat_id, message)
                delivered = bool(sent)
                if not delivered:
                    logger.warning(
                        "Telegram alert send returned no sent messages (title=%r)", title
                    )
    except Exception as e:
        logger.warning("Alert delivery to Telegram failed: %s", e)

    if not delivered:
        # The page was lost — leave a durable trace the briefing will surface.
        await _write_notification("alert_fallback", level, title, body, None)
    return delivered


async def _write_notification(
    notification_type: str,
    level: str,
    title: str,
    body: str,
    metadata: dict[str, Any] | None,
) -> bool:
    """Write the alert into ``crm_agent_notifications`` addressed to main.

    The main agent's heartbeat and the morning briefing read this inbox, so
    a row here reaches the operator on the next cycle without a page.
    """
    try:
        from robothor.crm.dal import send_notification

        notif_id = await asyncio.to_thread(
            send_notification,
            from_agent="engine",
            to_agent="main",
            notification_type=notification_type,
            subject=f"[{level}] {title}",
            body=body,
            metadata=metadata,
        )
        if notif_id is None:
            logger.warning(
                "Alert notification write refused (type=%s, title=%r)", notification_type, title
            )
            return False
        return True
    except Exception as e:
        logger.warning(
            "Alert notification write failed (type=%s, title=%r): %s",
            notification_type,
            title,
            e,
        )
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
