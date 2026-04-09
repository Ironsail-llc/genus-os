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
from typing import TYPE_CHECKING, Any

from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Platform sender registry — populated by daemon on startup.
_platform_senders: dict[str, Any] = {}


def register_platform_sender(platform: str, send_func: Callable[..., Any]) -> None:
    """Register a send function for a delivery platform."""
    _platform_senders[platform] = send_func
    logger.info("Registered platform sender: %s", platform)


def get_platform_sender(platform: str) -> Any | None:
    """Get the registered send function for a platform."""
    return _platform_senders.get(platform)


def set_telegram_sender(send_func: Callable[..., Any]) -> None:
    """Register the Telegram send function (called by daemon on startup)."""
    register_platform_sender("telegram", send_func)


def get_telegram_sender() -> Callable[..., Any] | None:
    """Get the registered Telegram send function (or None)."""
    return get_platform_sender("telegram")


async def _persist_delivery_status(run: AgentRun) -> None:
    """Persist delivery status to DB after deliver() modifies the in-memory run.

    This is needed because _persist_run() in the runner may have already saved the
    run to DB before deliver() sets delivery_status/delivered_at/delivery_channel.
    Idempotent — safe to call even if the run hasn't been persisted yet.
    """
    if not run.id or not run.delivery_status:
        return
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_runs
                   SET delivery_status = %s, delivered_at = %s, delivery_channel = %s
                   WHERE id = %s""",
                (run.delivery_status, run.delivered_at, run.delivery_channel, run.id),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to persist delivery status for run %s", run.id)


async def deliver(config: AgentConfig, run: AgentRun) -> bool:
    """Deliver agent output based on the delivery mode.

    Returns True if delivery succeeded.
    """
    # Sub-agent output should never reach Telegram (belt-and-suspenders)
    if run.parent_run_id is not None:
        logger.debug("Suppressing delivery for sub-agent run %s", run.id)
        run.delivery_status = "suppressed_sub_agent"
        await _persist_delivery_status(run)
        return True

    # ── [HOOKS] PRE_DELIVERY lifecycle hook ──
    try:
        from robothor.engine.hook_registry import (
            HookAction,
            HookContext,
            HookEvent,
            get_hook_registry,
        )

        hr = get_hook_registry()
        if hr and run.output_text:
            pre_ctx = HookContext(
                event=HookEvent.PRE_DELIVERY,
                agent_id=config.id,
                run_id=run.id,
                output_text=run.output_text or "",
            )
            pre_result = await hr.dispatch(HookEvent.PRE_DELIVERY, pre_ctx)
            if pre_result.action == HookAction.BLOCK:
                logger.info("Delivery blocked by hook for %s: %s", config.id, pre_result.reason)
                run.delivery_status = f"blocked_by_hook:{pre_result.reason}"
                await _persist_delivery_status(run)
                return True
    except Exception as e:
        logger.warning("PRE_DELIVERY hook error: %s", e)

    if not run.output_text:
        if run.error_message:
            # Always notify the user when a run failed — never silently swallow errors
            run.output_text = f"\u26a0\ufe0f Task incomplete \u2014 {run.error_message}"
        else:
            logger.debug("No output to deliver for %s", config.id)
            run.delivery_status = "no_output"
            await _persist_delivery_status(run)
            return True

    # Strip any trailing HEARTBEAT_OK the LLM may hallucinate
    text = run.output_text.strip()
    text = text.removesuffix("HEARTBEAT_OK").strip()
    if not text:
        logger.debug("Output was only HEARTBEAT_OK for %s, treating as no output", config.id)
        run.delivery_status = "no_output"
        await _persist_delivery_status(run)
        return True

    mode = config.delivery_mode

    if mode == DeliveryMode.NONE:
        logger.debug("Delivery mode=none for %s, skipping", config.id)
        run.delivery_status = "silent"
        await _persist_delivery_status(run)
        return True

    if mode == DeliveryMode.ANNOUNCE:
        result = await _deliver_telegram(config, text, run)
        await _persist_delivery_status(run)
        return result

    if mode == DeliveryMode.LOG:
        result = await _deliver_event_bus(config, text, run)
        await _persist_delivery_status(run)
        return result

    logger.warning("Unknown delivery mode %s for %s", mode, config.id)
    run.delivery_status = f"unknown_mode:{mode}"
    await _persist_delivery_status(run)
    return False


async def _deliver_telegram(config: AgentConfig, text: str, run: AgentRun) -> bool:
    """Send output to Telegram (uses platform registry)."""
    sender = get_platform_sender("telegram")
    if sender is None:
        logger.warning("Telegram sender not initialized, can't deliver for %s", config.id)
        return False

    chat_id = config.delivery_to
    if not chat_id:
        logger.warning("No delivery_to chat ID for %s", config.id)
        return False
    if "${" in chat_id:
        logger.error("Unexpanded env var in delivery_to for %s: %s", config.id, chat_id)
        return False

    try:
        header = f"*{config.name}*\n\n"
        full_text = header + text

        await sender(chat_id, full_text)

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
