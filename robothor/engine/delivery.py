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


_TRIVIAL_PATTERNS = [
    "all clear",
    "all quiet",
    "nothing new",
    "board is clean",
    "no open tasks",
    "standing down",
    "no updates",
    "nothing to report",
    "inbox empty",
    "fleet clean",
    "no new activity",
    "board unchanged",
    "no changes",
    "no movement",
    "nothing actionable",
]


def _is_heartbeat_run(run: AgentRun) -> bool:
    """Check if this run came from a heartbeat trigger."""
    return bool(run.trigger_detail and run.trigger_detail.startswith("heartbeat:"))


def _is_trivial_output(text: str) -> bool:
    """Detect 'nothing to report' output that shouldn't be delivered.

    Short messages (<300 chars) containing common filler phrases are suppressed.
    Substantial reports always get through.
    """
    if len(text) > 300:
        return False
    lower = text.lower()
    return any(p in lower for p in _TRIVIAL_PATTERNS)


# ── Buddy reflection (subconscious one-liner) ────────────────────────────


def _get_buddy_context() -> dict[str, Any] | None:
    """Get buddy events gated by cooldowns.

    Returns context only when there are new, non-cooldown events.
    Calling this consumes eligible events (marks their cooldowns).
    """
    try:
        from robothor.engine.buddy import BuddyEngine

        ctx = BuddyEngine().get_buddy_events()
        if not ctx or not ctx.get("events"):
            return None
        return ctx
    except Exception:
        logger.debug("Failed to get buddy context for reflection")
        return None


async def _generate_buddy_reflection(heartbeat_text: str, buddy_ctx: dict[str, Any]) -> str | None:
    """Generate a buddy reflection via a lightweight LLM call.

    Returns a short reflection (2-3 sentences), or None if buddy decides to stay silent.
    """
    events_str = ", ".join(buddy_ctx.get("events", []))
    overall = buddy_ctx.get("overall_score", 50)
    streak = buddy_ctx.get("streak", (0, 0))
    deltas = buddy_ctx.get("score_deltas", {})
    level_info = buddy_ctx.get("level_info")
    level_str = f"Level {level_info.level} {level_info.level_name}" if level_info else "Unknown"

    fleet_top = buddy_ctx.get("fleet_top", [])
    fleet_str = (
        ", ".join(f"{a['agent_id']}={a['overall_score']}" for a in fleet_top[:3])
        if fleet_top
        else "no fleet data"
    )

    # Format deltas as readable string
    delta_parts = []
    for dim in ("reliability", "debugging", "patience", "wisdom", "chaos"):
        d = deltas.get(dim, 0)
        if d != 0:
            delta_parts.append(f"{dim} {'+' if d > 0 else ''}{d}")
    deltas_str = ", ".join(delta_parts) if delta_parts else "no changes"

    prompt = (
        "You are Buddy — the fleet's subconscious. Something notable just happened. "
        "Reflect in exactly 2 sentences. Be specific about scores and agents. "
        "If you have nothing insightful to add, return ONLY the word SILENT.\n\n"
        f"Fleet: {level_str} | {streak[0]}-day streak | overall: {overall}\n"
        f"Changes: {deltas_str}\n"
        f"Top agents: {fleet_str}\n"
        f"Events: {events_str}\n\n"
        "2 sentences:"
    )

    try:
        from robothor.engine.llm_client import llm_call

        response = await llm_call(
            messages=[{"role": "user", "content": prompt}],
            model="openrouter/xiaomi/mimo-v2-pro",
            temperature=0.7,
            max_tokens=300,
            timeout=15,
        )
        text = response.choices[0].message.content.strip()
        if not text or text.upper() == "SILENT":
            return None
        return text
    except Exception as e:
        logger.debug("Buddy reflection LLM call failed: %s", e)
        return None


async def _maybe_append_buddy_reflection(
    text: str, run: AgentRun, config: AgentConfig
) -> tuple[str, bool]:
    """Optionally append a buddy one-liner to heartbeat output.

    Only fires for the main agent's heartbeat runs. Stays silent when
    there are no noteworthy events.

    Returns (text, has_reflection) tuple.
    """
    if not _is_heartbeat_run(run):
        return text, False
    if config.id != "main":
        return text, False

    ctx = _get_buddy_context()
    if not ctx:
        return text, False

    reflection = await _generate_buddy_reflection(text, ctx)
    if reflection:
        return f"{text}\n\n---\n{reflection}", True
    return text, False


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

    text = run.output_text.strip()

    # Buddy reflection — append subconscious one-liner to main heartbeat
    text, has_reflection = await _maybe_append_buddy_reflection(text, run, config)

    # Suppress trivial heartbeat output — short filler like "All quiet" or "Nothing new"
    # Skip suppression if buddy added a reflection (it decided something was worth saying)
    if _is_heartbeat_run(run) and _is_trivial_output(text) and not has_reflection:
        logger.debug("Suppressed trivial heartbeat output for %s: %s", config.id, text[:80])
        run.delivery_status = "suppressed_trivial"
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
