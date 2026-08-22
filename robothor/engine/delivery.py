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

from robothor.engine.chunking import split_telegram_message
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

    Signals:
    - Ends with a colon, ellipsis, or dash (classic "and then..." tail).
    - Starts with a mid-action tell ("Good —", "Now let me", "I'll").
    - Starts with a conversational back-reference that only makes sense
      mid-stream ("The verification flags are...", "These issues...").

    Substantial output (>300 chars) with a clean ending is real content
    even if it happens to start with a narration leader — heartbeat
    digests routinely lead with one CoT sentence before the structured
    body. Only flag short OR mid-ending text. ``_strip_cot_prefix`` is
    the primary defense for the leak; this is the safety net.
    """
    stripped = text.strip()
    if not stripped:
        return False
    tail = stripped.rstrip()
    ends_mid = tail.endswith((":", "…", "...", "—"))
    lower = stripped.lower()
    leads_mid = any(lower.startswith(p) for p in _MID_THOUGHT_LEADERS)
    if len(stripped) > 300 and not ends_mid:
        return False
    return ends_mid or leads_mid


# Additional CoT prefix leaders observed in production heartbeat output —
# beyond the _MID_THOUGHT_LEADERS set, the model also opens with these
# patterns despite explicit prompt instructions not to narrate.
_COT_PREFIX_LEADERS: tuple[str, ...] = (
    "now i have",
    "i have all",
    "i have everything",
    "now i'm",
    "now composing",
    "fleet delta is",
)

# Tail markers that indicate a real structured digest follows — used by
# _strip_cot_prefix to confirm the post-prefix content is the actual
# operator-facing report and not a second prose paragraph.
_STRUCTURED_TAIL_PREFIXES: tuple[str, ...] = (
    "⚡",
    "🎯",
    "🤝",
    "📊",
    "✅",
    "⚠️",
    "🔵",
    "🟢",
    "🔴",
    "##",
    "#",
    "- ",
    "· ",
    "* ",
    "**",
)


def _strip_cot_prefix(text: str) -> str:
    """Strip a leading chain-of-thought paragraph if a structured digest
    follows.

    MiMo V2.5 Pro reliably emits one short narration sentence before the
    actual heartbeat digest ("Now let me compose the digest.\\n\\n⚡ SAT
    ...") despite ``brain/HEARTBEAT.md`` forbidding it. That prefix
    trips the mid-thought heuristic and gets the entire digest reframed
    as ``⚠️ Beat ended incomplete``. This function pulls the narration
    off so the operator sees the digest the model actually produced.

    Conservative — only strips when ALL of these hold:
    - There is a ``\\n\\n`` paragraph break.
    - The head is ≤200 chars (a real prefix, not a body paragraph).
    - The head ends with ``.``, ``:``, or ``…`` (sentence-final).
    - The head's lowercased start matches a known narration leader
      (reuses ``_MID_THOUGHT_LEADERS`` plus ``_COT_PREFIX_LEADERS``).
    - The tail starts with a structured marker (emoji, header, bullet)
      indicating the actual digest follows.

    A genuine fragment like ``"Now let me check the inbox—"`` (no
    structured tail) is left alone so the existing reframe path still
    catches it.
    """
    if not text:
        return text
    parts = text.split("\n\n", 1)
    if len(parts) != 2:
        return text
    head, tail = parts[0], parts[1]
    head_stripped = head.strip()
    if not head_stripped or len(head_stripped) > 200:
        return text
    if not head_stripped.endswith((".", ":", "…")):
        return text
    lower = head_stripped.lower()
    looks_like_narration = any(
        lower.startswith(p) for p in (*_MID_THOUGHT_LEADERS, *_COT_PREFIX_LEADERS)
    )
    if not looks_like_narration:
        return text
    tail_stripped = tail.lstrip()
    if not tail_stripped.startswith(_STRUCTURED_TAIL_PREFIXES):
        return text
    return tail_stripped


def _beat_incomplete(run: AgentRun, text: str | None = None) -> bool:
    """Return True when the run ended in a degenerate state that shouldn't
    ship its raw output_text.

    Currently: hard-budget exhaustion, a trailing ``error`` step (hard
    timeout, stall, model failure), or mid-thought narration in
    output_text. These cases get re-framed by ``_reframe_beat_output``.

    Pass ``text`` to override which body is checked for mid-thought
    narration (delivery passes the CoT-stripped body so the heuristic
    sees the real digest, not the model's narration prefix). The
    budget-exhausted and error-step checks are run-state, not text-
    state, and always apply.
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
    check_text = text if text is not None else run.output_text
    return bool(check_text and _looks_like_mid_thought(check_text))


def _beat_incomplete_text(run: AgentRun, text: str) -> bool:
    """Convenience wrapper used by ``deliver()`` after CoT-prefix stripping."""
    return _beat_incomplete(run, text=text)


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


def _verification_banner(run: AgentRun) -> str:
    """Return an honest-failure banner when the run claimed work it can't show.

    Delivery-time only, exactly like ``_reframe_beat_output`` and
    ``_strip_cot_prefix`` above: ``agent_runs.output_text`` keeps the raw claim
    because the raw claim IS the evidence. What changes is that the operator
    reading "✅ Payment confirmed" also reads that nothing in the run's tool
    trace backs it.

    Only the ``unverified_claims`` class earns a banner — that is the "nothing
    was even attempted" verdict (the Venmo shape). ``failed_verification``
    means a real tool call was made and failed, which the agent's own output
    and the error path already surface.

    Flag-gated on ``ROBOTHOR_RUN_VERIFICATION_MODE``: silent at ``off`` and
    ``observe``, present at ``alert`` and ``enforce``. Never raises.
    """
    try:
        from robothor.engine.feature_flags import run_verification_mode

        if run_verification_mode() not in ("alert", "enforce"):
            return ""
        if getattr(run, "verified_status", None) != "unverified_claims":
            return ""
        from robothor.engine.run_verification import describe_unsupported

        described = describe_unsupported(getattr(run, "verification", None))
        if not described:
            return ""
        return (
            f"⚠️ Unverified: I claimed to {described}, but no tool "
            "call in this run shows it happened."
        )
    except Exception as e:  # noqa: BLE001 — a banner must never block delivery
        logger.debug("verification banner skipped: %s", e)
        return ""


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

    # Strip leading chain-of-thought narration before any incomplete-check.
    # The model leaks one short narration sentence ("Now let me compose
    # the digest.") in front of the actual digest in ~70% of heartbeats;
    # without stripping, that prefix tripped _looks_like_mid_thought and
    # the entire digest was replaced with "⚠️ Beat ended incomplete".
    # The raw output_text in agent_runs is left untouched for debugging;
    # only the delivered body is rewritten.
    raw_text = run.output_text or ""
    if _is_heartbeat_run(run):
        delivered_source = _strip_cot_prefix(raw_text)
    else:
        delivered_source = raw_text

    # Re-frame heartbeat output when the beat ended incomplete — otherwise
    # the operator gets a fragment of mid-chain-of-thought ("Now let me
    # send it using the GWS tools:") and has no idea what actually
    # happened. The raw output_text stays in agent_runs; only the
    # delivered body is swapped.
    if _is_heartbeat_run(run) and _beat_incomplete_text(run, delivered_source):
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
        text = delivered_source.strip()

    # Suppress trivial heartbeat output — short filler like "All quiet" or "Nothing new"
    if _is_heartbeat_run(run) and _is_trivial_output(text):
        logger.debug("Suppressed trivial heartbeat output for %s: %s", config.id, text[:80])
        run.delivery_status = "suppressed_trivial"
        await _persist_delivery_status(run)
        return True

    # Honest-failure banner. Appended AFTER the trivial-output check so it can
    # never resurrect a beat that was meant to stay silent, and to the
    # delivered body only — run.output_text keeps the raw claim as evidence.
    banner = _verification_banner(run)
    if banner:
        text = f"{text}\n\n{banner}" if text else banner

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


def _mark_delivery_failed(run: AgentRun, channel: str, status: str) -> None:
    """Record a delivery failure on the run so it is never mistaken for a send.

    Args:
        run: The run whose delivery failed.
        channel: The channel that was attempted (``telegram``, ``event_bus``).
        status: The ``delivery_status`` value to record; always prefixed
            ``failed:`` so consumers can match it.
    """
    run.delivery_status = status
    run.delivery_channel = channel
    run.delivered_at = None


def _acknowledged_messages(sent: Any) -> tuple[int, list[str]]:
    """Count the chunks the platform actually acknowledged.

    ``TelegramBot.send_message`` returns one entry per chunk it managed to
    send — a chunk that failed both the HTML and the plain-text attempt is
    simply absent from the list, so the length of the result is the only
    evidence of what landed.

    Args:
        sent: Whatever the registered sender returned.

    Returns:
        ``(acknowledged_count, platform_message_ids)``. The id list can be
        shorter than the count if the platform returned an object without a
        ``message_id``; the count, not the ids, decides delivery.
    """
    if not sent:
        return 0, []
    try:
        messages = list(sent)
    except TypeError:  # a single message object, not a sequence
        messages = [sent]

    count = 0
    message_ids: list[str] = []
    for msg in messages:
        if msg is None:
            continue
        count += 1
        mid = getattr(msg, "message_id", None)
        if mid is not None:
            message_ids.append(str(mid))
    return count, message_ids


async def _deliver_telegram(config: AgentConfig, text: str, run: AgentRun) -> bool:
    """Send output to Telegram and record what actually happened.

    ``TelegramBot.send_message`` never raises: it retries a failed chunk as
    plain text and, when that fails too, logs the error and returns a list
    that is simply *missing* that chunk (empty if every chunk failed). The
    returned list is therefore the only evidence of delivery, so the status
    is derived from it rather than assumed — the same discipline
    ``alerts.py`` adopted after the arity bug sent 432+ pages nowhere:

    - every chunk acknowledged: ``delivered``, with ``delivered_at`` set
    - some chunks acknowledged: ``partial:<sent>/<expected>``
    - nothing acknowledged: ``failed:telegram_send``

    ``delivered_at`` is set only on a complete send — a truncated briefing
    is not a delivered briefing.

    Args:
        config: The agent config supplying the chat id and display name.
        text: The already-processed body to deliver.
        run: The run to stamp with the delivery outcome.

    Returns:
        True only if every chunk was acknowledged by the platform.
    """
    sender = get_platform_sender("telegram")
    if sender is None:
        logger.warning("Telegram sender not initialized, can't deliver for %s", config.id)
        _mark_delivery_failed(run, "telegram", "failed:telegram_no_sender")
        return False

    chat_id = config.delivery_to
    if not chat_id:
        logger.warning("No delivery_to chat ID for %s", config.id)
        _mark_delivery_failed(run, "telegram", "failed:telegram_no_chat_id")
        return False
    if "${" in chat_id:
        logger.error("Unexpanded env var in delivery_to for %s: %s", config.id, chat_id)
        _mark_delivery_failed(run, "telegram", "failed:telegram_unexpanded_chat_id")
        return False

    header = f"*{config.name}*\n\n"
    full_text = header + text
    expected_chunks = len(split_telegram_message(full_text))

    try:
        sent = await sender(chat_id, full_text)
    except Exception as e:
        logger.error("Telegram delivery failed for %s: %s", config.id, e)
        _mark_delivery_failed(run, "telegram", f"failed:telegram_exception: {e}")
        return False

    acknowledged, platform_message_ids = _acknowledged_messages(sent)

    if acknowledged == 0:
        logger.error(
            "Telegram delivery for %s acknowledged 0 of %d chunk(s) — "
            "the operator saw nothing (run=%s)",
            config.id,
            expected_chunks,
            run.id,
        )
        _mark_delivery_failed(run, "telegram", "failed:telegram_send")
        return False

    complete = acknowledged >= expected_chunks
    run.delivery_channel = "telegram"
    if complete:
        run.delivery_status = "delivered"
        run.delivered_at = datetime.now(UTC)
    else:
        run.delivery_status = f"partial:{acknowledged}/{expected_chunks}"
        run.delivered_at = None
        logger.error(
            "Telegram delivery for %s was truncated: %d of %d chunk(s) landed (run=%s)",
            config.id,
            acknowledged,
            expected_chunks,
            run.id,
        )

    # Map the chunks that DID land so reply-to resolution still works for them.
    await _dispatch_post_delivery(
        config=config,
        run=run,
        text=full_text,
        channel="telegram",
        chat_id=chat_id,
        platform_message_ids=platform_message_ids,
    )
    return complete


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
    """Publish output to the Redis event bus and record the real outcome.

    ``events.bus.publish`` returns the stream message id, or ``None`` when
    the bus is disabled, Redis is unreachable, or the write was refused — it
    never raises. Writing ``published`` without checking that id is a guess,
    so the id decides the status.

    Args:
        config: The agent config (supplies the agent id in the payload).
        text: The already-processed body to publish.
        run: The run to stamp with the delivery outcome.

    Returns:
        True only if the bus returned a stream message id.
    """
    try:
        from robothor.events import bus

        if not bus.EVENT_BUS_ENABLED:
            logger.warning(
                "Event bus disabled — output for %s was not published (run=%s)",
                config.id,
                run.id,
            )
            _mark_delivery_failed(run, "event_bus", "failed:event_bus_disabled")
            return False

        msg_id = bus.publish(
            stream="agent",
            event_type="agent.run.output",
            payload={
                "agent_id": config.id,
                "run_id": run.id,
                "output": text[:2000],
                "status": run.status.value,
            },
        )
    except Exception as e:
        logger.warning("Event bus delivery failed for %s: %s", config.id, e)
        _mark_delivery_failed(run, "event_bus", f"failed:event_bus_exception: {e}")
        return False

    if not msg_id:
        logger.error(
            "Event bus publish for %s returned no message id — output was not published (run=%s)",
            config.id,
            run.id,
        )
        _mark_delivery_failed(run, "event_bus", "failed:event_bus_publish")
        return False

    run.delivery_status = "published"
    run.delivery_channel = "event_bus"
    return True
