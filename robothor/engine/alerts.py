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

from robothor.constants import BENCHMARK_DIGEST_NOTIFICATION_TYPE

logger = logging.getLogger(__name__)

# Alert levels that page Telegram immediately. Everything else goes to the
# digest so the operator is only interrupted for things that need them.
_PAGE_LEVELS = frozenset({"critical"})

#: Where an alert raised INSIDE a benchmark child goes. A separate notification
#: type rather than a tag on ``alert_digest``, because the heartbeat's alert
#: reader selects by type (``warmup.ALERT_DIGEST_TYPES``) and a tag it does not
#: read is a tag it cannot act on. On 2026-09-11 the graded runs produced 135
#: digest rows in fourteen hours and the operator's "Hello" became a six-minute
#: triage of alerts about agents that were being TESTED, not failing.
#:
#: The rows are still written, and deliberately: a benchmark suite that trips
#: the runaway-token guard every night is a real finding about the suite. It is
#: simply not an interrupt.
#: Canonical value lives in :mod:`robothor.constants` because ``crm/dal.py``
#: must exclude it from ``get_agent_inbox`` without importing the engine.
BENCHMARK_DIGEST_TYPE = BENCHMARK_DIGEST_NOTIFICATION_TYPE

#: Prefix on the subject of such a row, so a human reading the table directly
#: can tell at a glance.
BENCHMARK_SUBJECT_TAG = "[benchmark]"


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
    from robothor.engine.run_context import in_benchmark_run

    if in_benchmark_run():
        # A graded child is not production. Whatever the level, whatever the
        # channel: no page, no webhook, and a row in a type the operator's
        # heartbeat does not read. Checked before the channel switch so a
        # future channel cannot be added past it.
        return await _write_notification(
            BENCHMARK_DIGEST_TYPE,
            level,
            f"{BENCHMARK_SUBJECT_TAG} {title}",
            body,
            metadata,
        )

    if channel == "telegram":
        if level in _PAGE_LEVELS:
            return await _send_telegram(level, title, body)
        return await _write_notification("alert_digest", level, title, body, metadata)
    elif channel == "webhook":
        return await _send_webhook(level, title, body, metadata)
    else:
        logger.warning("Unknown alert channel: %s", channel)
        return False


async def alert_about_run(
    level: str,
    title: str,
    body: str,
    *,
    trigger_detail: str | None = None,
    is_benchmark: bool = False,
    channel: str = "telegram",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Alert ABOUT a run, routed by whether that run is benchmark traffic.

    :func:`alert` routes on the CURRENT task's context, which is right for
    anything raised inside the run. The out-of-band detectors
    (:mod:`robothor.engine.detectors`) are the other case: they run on the
    daemon's periodic loop, select ``agent_runs`` rows, and alert about a run
    they are not inside — so ``in_benchmark_run()`` is False there however
    benchmark the subject is, and 135 digests about agents that were being
    GRADED reached the operator's heartbeat exactly as before.

    Pass the subject run's ``trigger_detail`` (and its ``is_benchmark`` flag
    where the row carries one) and the routing follows the subject rather than
    the caller. With neither, this is plain :func:`alert` — so a detector that
    genuinely has no run row (a workflow, a tool, a model) keeps today's
    behaviour by calling the same helper.
    """
    from robothor.engine.analytics import is_benchmark_run

    if is_benchmark_run(trigger_detail, is_benchmark=is_benchmark):
        return await _write_notification(
            BENCHMARK_DIGEST_TYPE,
            level,
            f"{BENCHMARK_SUBJECT_TAG} {title}",
            body,
            metadata,
        )
    return await alert(level, title, body, channel=channel, metadata=metadata)


def note_benchmark_runaway(agent_id: str, run_id: str, tokens: int, model_used: str | None) -> None:
    """Record a soft runaway-token crossing from a GRADED run, unbatched.

    The production path batches soft crossings so six catch-up runs do not
    become six pages. That batch is module-global state in the runner, flushed
    by whichever run crosses next and in that run's context — so a benchmark
    child must not join it: flushing a batch of production crossings from
    inside a graded run would write the whole summary as ``benchmark_digest``,
    which the heartbeat does not read and nothing acknowledges, and a genuine
    runaway page would be silently lost.

    A graded crossing is therefore reported immediately and on its own.
    :func:`alert` sees the marker and files it as ``benchmark_digest``, so it
    is recorded and interrupts nobody. The per-run ``logger.warning`` at the
    call site and the hard cap are unaffected — only the page is decided here.

    Sync and fire-and-forget, mirroring ``_send_soft_runaway_alert``: it is
    called from the run loop, which must not await an alert.
    """
    from robothor.engine.task_registry import get_task_registry

    get_task_registry().spawn(
        alert(
            "warning",
            f"Runaway-token alert: {agent_id}",
            f"run_id={run_id} tokens={tokens:,} model={model_used} (benchmark run)",
        ),
        name=f"runaway-alert-benchmark:{agent_id}",
    )


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
