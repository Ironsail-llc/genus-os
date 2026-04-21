"""
Output delivery — routes agent output to the correct destination.

Modes:
- announce: Send to Telegram chat
- none: Silent (no delivery)
- log: Publish to event bus only
"""

from __future__ import annotations

import logging
import re
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
    Uses word-boundary matching to avoid false positives on substrings.
    Substantial reports always get through.
    """
    if len(text) > 300:
        return False
    lower = text.lower()
    return any(re.search(r"\b" + re.escape(p) + r"\b", lower) for p in _TRIVIAL_PATTERNS)


# ── Mid-thought / incomplete-beat detection ──────────────────────────
# When a heartbeat hits a budget cap or gets cancelled, the last LLM
# turn is often mid-chain-of-thought narration ("Now let me send it
# using the GWS tools:"). Shipping that as the beat report gaslights
# the operator — they get an unfinished sentence, not a status. These
# heuristics detect that case so deliver() can re-frame it.

_MID_THOUGHT_LEADERS = (
    "good —",
    "good,",
    "okay —",
    "okay,",
    "ok,",
    "ok —",
    "alright",
    "right —",
    "now let me",
    "let me now",
    "let me",
    "i'll ",
    "i will ",
    "now i'll",
    "now i will",
    "next, i'll",
    "next i'll",
    "first, i'll",
    "first i'll",
    "so i'll",
    "going to",
    # Back-references to prior-beat content — only make sense mid-conversation.
    # Real first-turn beat reports don't open with "the X are...", "these Y...".
    "the verification",
    "the flags",
    "these issues",
    "these findings",
    "these errors",
    "these results",
    "all 3",
    "all three",
    "all four",
    "all five",
    "next step",
    "next steps",
    "finally,",
)


def _looks_like_mid_thought(text: str) -> bool:
    """Heuristic: the model's final turn was narration about what it
    was *about to do*, not a summary of what it did.

    Any of these signals on its own is enough (previously required BOTH):
    - Ends with a colon, ellipsis, or dash (classic "and then..." tail).
    - Starts with a mid-action tell ("Good —", "Now let me", "I'll").
    - Starts with a conversational back-reference that only makes sense
      mid-stream ("The verification flags are...", "These issues...").

    Tightening the net here: false positives become reframed diagnostics,
    which is still better for the operator than shipping a fragment.
    """
    stripped = text.strip()
    if not stripped:
        return False
    tail = stripped.rstrip()
    ends_mid = tail.endswith((":", "…", "...", "—"))
    lower = stripped.lower()
    leads_mid = any(lower.startswith(p) for p in _MID_THOUGHT_LEADERS)
    return ends_mid or leads_mid


def _beat_incomplete(run: AgentRun) -> bool:
    """Return True when the run ended in a degenerate state that shouldn't
    ship its raw output_text.

    Currently: hard-budget exhaustion, a trailing ``error`` step (hard
    timeout, stall, model failure), or mid-thought narration in
    output_text. These cases get re-framed by ``_reframe_beat_output``.
    """
    if getattr(run, "budget_exhausted", False):
        return True
    steps = getattr(run, "steps", None) or []
    if steps and getattr(steps[-1], "step_type", None):
        st = steps[-1].step_type
        # Support StepType enum or raw string
        st_val = getattr(st, "value", st)
        if str(st_val) == "error":
            return True
    return bool(run.output_text and _looks_like_mid_thought(run.output_text))


def _reframe_beat_output(run: AgentRun) -> str:
    """Build a structured status line for an incomplete heartbeat beat.

    Replaces output_text for delivery ONLY — the raw model output is
    still persisted in agent_runs.output_text for debugging. The goal
    is that the operator sees a diagnostic, not a fragment of
    mid-chain-of-thought.
    """
    steps = getattr(run, "steps", None) or []

    # Tally tool calls by name.
    tool_counts: dict[str, int] = {}
    last_tool = ""
    for s in steps:
        st = getattr(s, "step_type", None)
        st_val = str(getattr(st, "value", st))
        if st_val == "tool_call":
            name = getattr(s, "tool_name", "") or "?"
            tool_counts[name] = tool_counts.get(name, 0) + 1
            last_tool = name

    # Identify the failure mode.
    err = ""
    if getattr(run, "budget_exhausted", False):
        err = "Budget cap reached before finishing."
    elif steps:
        last = steps[-1]
        st_val = str(getattr(getattr(last, "step_type", None), "value", ""))
        if st_val == "error":
            err = (getattr(last, "error_message", "") or "Run ended in error step.").strip()

    lines = [
        f"\u26a0\ufe0f Beat ended incomplete: {err}"
        if err
        else "\u26a0\ufe0f Beat ended mid-action."
    ]
    if tool_counts:
        summary = ", ".join(f"{name}:{n}" for name, n in sorted(tool_counts.items()))
        lines.append(f"Tools completed ({sum(tool_counts.values())}): {summary}")
    if last_tool:
        lines.append(f"Last completed action: {last_tool}")
    if run.output_text:
        tail = run.output_text.strip()
        if len(tail) > 400:
            tail = tail[-400:]
        lines.append("Model was about to say (truncated):")
        lines.append(tail)
    return "\n".join(lines)


async def deliver(config: AgentConfig, run: AgentRun) -> bool:
    """Deliver agent output based on the delivery mode.

    Returns True if delivery succeeded.
    """
    # Outcome-driven fact invalidation: when a run failed, bump outcome_failures
    # on every fact that was retrieved during the run. Best-effort, fire-and-forget
    # via asyncio.to_thread so delivery isn't blocked.
    try:
        run_failed = bool(run.error_message) or (
            getattr(run, "status", None) is not None
            and str(getattr(run.status, "value", run.status)).upper() == "FAILED"
        )
        if run_failed and run.id:
            from robothor.memory.outcomes import bump_failure_for_run

            tenant_id = getattr(run, "tenant_id", None) or getattr(config, "tenant_id", None)
            import asyncio as _aio

            await _aio.to_thread(bump_failure_for_run, str(run.id), tenant_id)
    except Exception as e:
        logger.debug("Outcome attribution failed (non-fatal): %s", e)

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

    # Re-frame heartbeat output when the beat ended incomplete — otherwise
    # the operator gets a fragment of mid-chain-of-thought ("Now let me
    # send it using the GWS tools:") and has no idea what actually
    # happened. The raw output_text stays in agent_runs; only the
    # delivered body is swapped.
    if _is_heartbeat_run(run) and _beat_incomplete(run):
        reframed = _reframe_beat_output(run)
        logger.info(
            "Heartbeat reframed for %s: budget=%s last_step_err=%s",
            config.id,
            getattr(run, "budget_exhausted", False),
            bool(
                run.steps
                and str(getattr(getattr(run.steps[-1], "step_type", None), "value", "")) == "error"
            )
            if getattr(run, "steps", None)
            else False,
        )
        text = reframed
    else:
        text = run.output_text.strip()

    # Suppress trivial heartbeat output — short filler like "All quiet" or "Nothing new"
    if _is_heartbeat_run(run) and _is_trivial_output(text):
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

        sent = await sender(chat_id, full_text)

        run.delivery_status = "delivered"
        run.delivered_at = datetime.now(UTC)
        run.delivery_channel = "telegram"

        platform_message_ids: list[str] = []
        if sent:
            for msg in sent:
                mid = getattr(msg, "message_id", None)
                if mid is not None:
                    platform_message_ids.append(str(mid))

        await _dispatch_post_delivery(
            config=config,
            run=run,
            text=full_text,
            channel="telegram",
            chat_id=chat_id,
            platform_message_ids=platform_message_ids,
        )
        return True
    except Exception as e:
        logger.error("Telegram delivery failed for %s: %s", config.id, e)
        run.delivery_status = f"failed: {e}"
        return False


async def _dispatch_post_delivery(
    config: AgentConfig,
    run: AgentRun,
    text: str,
    channel: str,
    chat_id: str,
    platform_message_ids: list[str],
) -> None:
    """Fire the POST_DELIVERY lifecycle hook with channel-bus metadata.

    Best-effort: any failure here must not break delivery. The channel bus
    handler (robothor.engine.channel_bus.on_post_delivery) is the primary
    consumer.
    """
    try:
        from robothor.engine.hook_registry import (
            HookContext,
            HookEvent,
            get_hook_registry,
        )

        hr = get_hook_registry()
        if hr is None:
            return
        tenant_id = (
            getattr(run, "tenant_id", None) or getattr(config, "tenant_id", None) or "default"
        )
        ctx = HookContext(
            event=HookEvent.POST_DELIVERY,
            agent_id=config.id,
            run_id=run.id or "",
            output_text=text,
            metadata={
                "channel": channel,
                "chat_id": chat_id,
                "platform_message_ids": platform_message_ids,
                "author_display_name": config.name,
                "surface_to_channel": getattr(config, "surface_to_channel", True),
                "tenant_id": tenant_id,
                "trigger_detail": getattr(run, "trigger_detail", "") or "",
            },
        )
        await hr.dispatch(HookEvent.POST_DELIVERY, ctx)
    except Exception as e:
        logger.debug("POST_DELIVERY dispatch failed (non-fatal): %s", e)


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
