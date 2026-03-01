"""
Output delivery — routes agent output to the correct destination.

Modes:
- announce: Send to Telegram chat
- none: Silent (no delivery)
- log: Publish to event bus only
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode

logger = logging.getLogger(__name__)

# Will be set by daemon when TelegramBot initializes
_telegram_send = None


def set_telegram_sender(send_func) -> None:
    """Register the Telegram send function (called by daemon on startup)."""
    global _telegram_send
    _telegram_send = send_func


def get_telegram_sender():
    """Get the registered Telegram send function (or None)."""
    return _telegram_send


async def deliver(config: AgentConfig, run: AgentRun) -> bool:
    """Deliver agent output based on the delivery mode.

    Returns True if delivery succeeded.
    """
    # Sub-agent output should never reach Telegram (belt-and-suspenders)
    if run.parent_run_id is not None:
        logger.debug("Suppressing delivery for sub-agent run %s", run.id)
        return True

    if not run.output_text:
        logger.debug("No output to deliver for %s", config.id)
        return True

    # Suppress HEARTBEAT_OK messages
    text = run.output_text.strip()
    if text == "HEARTBEAT_OK":
        logger.debug("Suppressing HEARTBEAT_OK for %s", config.id)
        return True

    mode = config.delivery_mode

    if mode == DeliveryMode.NONE:
        logger.debug("Delivery mode=none for %s, skipping", config.id)
        return True

    if mode == DeliveryMode.ANNOUNCE:
        return await _deliver_telegram(config, text, run)

    if mode == DeliveryMode.LOG:
        return await _deliver_event_bus(config, text, run)

    logger.warning("Unknown delivery mode %s for %s", mode, config.id)
    return False


async def _deliver_telegram(config: AgentConfig, text: str, run: AgentRun) -> bool:
    """Send output to Telegram."""
    if _telegram_send is None:
        logger.warning("Telegram sender not initialized, can't deliver for %s", config.id)
        return False

    chat_id = config.delivery_to
    if not chat_id:
        logger.warning("No delivery_to chat ID for %s", config.id)
        return False

    try:
        # Prefix with agent name for context
        header = f"*{config.name}*\n\n"
        full_text = header + text

        await _telegram_send(chat_id, full_text)

        run.delivery_status = "delivered"
        run.delivered_at = datetime.now(UTC)
        run.delivery_channel = "telegram"
        return True
    except Exception as e:
        logger.error("Telegram delivery failed for %s: %s", config.id, e)
        run.delivery_status = f"failed: {e}"
        return False


async def _deliver_event_bus(config: AgentConfig, text: str, run: AgentRun) -> bool:
    """Publish output to the Redis event bus."""
    try:
        from robothor.events.bus import publish

        publish(
            stream="agent",
            event_type="agent.run.output",
            payload={
                "agent_id": config.id,
                "run_id": run.id,
                "output": text[:2000],
                "status": run.status.value,
            },
        )
        run.delivery_status = "published"
        run.delivery_channel = "event_bus"
        return True
    except Exception as e:
        logger.warning("Event bus delivery failed for %s: %s", config.id, e)
        run.delivery_status = f"failed: {e}"
        return False
