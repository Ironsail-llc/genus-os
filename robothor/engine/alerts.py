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
failure with an empty result list. A page Telegram cannot deliver (not
configured, or the send failed) is tried on each channel in
``ROBOTHOR_ALERT_FALLBACK_CHANNELS``, in order, through the channel registry;
a channel whose ``health()`` says it cannot send is skipped with a "would have
sent" line. Only when every channel fails is an ``alert_fallback``
notification row written, so the alert still reaches the next briefing, and
``genus doctor`` (``alerts.delivery``) reports the configuration that let it
happen. ``alert()`` returns whether delivery actually happened.

Usage::

    from robothor.engine.alerts import alert

    await alert("critical", "PostgreSQL down", "3 consecutive ping failures", channel="telegram")
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import time
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


#: ``alert_id -> monotonic time it was last delivered``. Only alerts raised with
#: an explicit id are suppressed; a repeat of one nobody received is a retry,
#: so the time is recorded on delivery, never on an attempt.
_delivered_at: dict[str, float] = {}


def parse_fallback_channels(raw: str) -> list[tuple[str, str]]:
    """``"email:a@example.com, slack:C1"`` -> ``[("email", ...), ("slack", "C1")]``.

    Split on the FIRST colon, so a target may carry colons of its own. An entry
    with no channel or no target is dropped with a warning: it names nowhere.
    """
    entries: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        name, _, target = item.partition(":")
        name, target = name.strip(), target.strip()
        if not name or not target:
            logger.warning("Ignoring alert fallback entry %r: expected <channel>:<target>", item)
            continue
        entries.append((name, target))
    return entries


def alert_fallback_channels() -> list[tuple[str, str]]:
    """The configured fallback channels, in the order they are tried."""
    from robothor.settings import get_settings

    return parse_fallback_channels(get_settings().engine.alert_fallback_channels)


async def channel_can_deliver(channel: Any) -> tuple[bool, str]:
    """Whether ``channel`` says it could send at all, and why not.

    Read from the channel's own ``health()``, which opens nothing for the
    built-ins that matter here (email reports whether a transport is
    configured). Channels report ``ok`` or, for sender shims,
    ``sender_registered``.
    """
    try:
        report = await channel.health()
    except Exception as exc:  # noqa: BLE001 — a health probe is a report
        return False, f"health check raised {type(exc).__name__}"
    if not isinstance(report, dict):
        return False, "health check returned no report"
    able = report.get("ok") if "ok" in report else report.get("sender_registered")
    if able:
        return True, ""
    return False, str(report.get("error") or "no send credentials configured")


def _cooldown_seconds() -> float:
    from robothor.settings import get_settings

    return float(get_settings().engine.alert_cooldown_seconds)


def _suppressed(alert_id: str | None) -> bool:
    if not alert_id:
        return False
    last = _delivered_at.get(alert_id)
    return last is not None and time.monotonic() - last < _cooldown_seconds()


def _record_delivery(alert_id: str | None, delivered: bool) -> bool:
    """Remember a delivered id, forget expired ones; returns ``delivered``."""
    if delivered and alert_id:
        now = time.monotonic()
        window = _cooldown_seconds()
        for stale in [key for key, at in _delivered_at.items() if now - at >= window]:
            del _delivered_at[stale]
        _delivered_at[alert_id] = now
    return delivered


def _derived_id(level: str, title: str) -> str:
    """A stable id for log lines when the caller gave none."""
    return hashlib.sha256(f"{level}:{title}".encode()).hexdigest()[:12]


async def alert(
    level: str,
    title: str,
    body: str,
    *,
    channel: str = "telegram",
    metadata: dict[str, Any] | None = None,
    alert_id: str | None = None,
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
        alert_id: Optional stable id. A repeat of a delivered alert with the
            same id inside ``ROBOTHOR_ALERT_COOLDOWN_SECONDS`` is not sent
            again. Without one, nothing is suppressed.

    Returns:
        True if the alert was verifiably delivered (Telegram or a fallback
        channel acknowledged it, or the digest notification row was written),
        or was delivered earlier and this repeat was suppressed.
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

    if _suppressed(alert_id):
        logger.info("Alert %s suppressed: delivered inside its cooldown window", alert_id)
        return True

    if channel == "telegram":
        if level in _PAGE_LEVELS:
            return await _page(level, title, body, alert_id)
        return _record_delivery(
            alert_id, await _write_notification("alert_digest", level, title, body, metadata)
        )
    elif channel == "webhook":
        return _record_delivery(alert_id, await _send_webhook(level, title, body, metadata))
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


async def _page(level: str, title: str, body: str, alert_id: str | None) -> bool:
    """Page the operator: Telegram first, then each configured fallback.

    The first channel that proves delivery ends the attempt, and one line says
    which it was. When none does, one ERROR line names every channel tried and
    an ``alert_fallback`` row is left for the next briefing.
    """
    shown_id = alert_id or _derived_id(level, title)
    if await _send_telegram(level, title, body):
        logger.info("Alert %s delivered via telegram", shown_id)
        return _record_delivery(alert_id, True)

    why = "is not configured" if not _telegram_configured() else "failed to deliver"
    tried = ["telegram"]
    for name, target in alert_fallback_channels():
        tried.append(name)
        if await _send_via_channel(name, target, level, title, body, shown_id):
            logger.warning(
                "Alert %s delivered via %s (fallback: the primary channel telegram %s)",
                shown_id,
                name,
                why,
            )
            return _record_delivery(alert_id, True)

    logger.error(
        "Alert %s reached no one: every alert channel failed (tried %s). "
        "Run `genus doctor --only alerts.delivery`.",
        shown_id,
        ", ".join(tried),
    )
    # The page was lost — leave a durable trace the briefing will surface.
    await _write_notification("alert_fallback", level, title, body, None)
    return False


def _telegram_configured() -> bool:
    """Whether the primary pager has both a registered sender and a chat id."""
    try:
        from robothor.engine.config import EngineConfig
        from robothor.engine.delivery import get_telegram_sender

        return get_telegram_sender() is not None and bool(EngineConfig.from_env().default_chat_id)
    except Exception:  # noqa: BLE001 — only decides a log line's wording
        return False


async def _send_via_channel(
    name: str, target: str, level: str, title: str, body: str, shown_id: str
) -> bool:
    """Deliver through one registry channel; True only on a complete receipt."""
    from robothor.engine.channels.registry import get_channel

    channel = get_channel(name)
    if channel is None:
        logger.warning("Alert fallback channel %r is not an available channel", name)
        return False
    able, reason = await channel_can_deliver(channel)
    if not able:
        logger.warning("would have sent alert %s via %s (%s)", shown_id, name, reason)
        return False

    from types import SimpleNamespace

    from robothor.constants import DEFAULT_TENANT

    # Channels that guard per-tenant state (the email opt-out list) read the
    # tenant off the run; an alert has no run, so it carries the default one.
    run = SimpleNamespace(id="", tenant_id=DEFAULT_TENANT, is_benchmark=False)
    try:
        receipt = await channel.send(
            target, f"[{level}] {title}\n\n{body}", subject=f"[{level}] {title}", run=run
        )
    except Exception as exc:  # noqa: BLE001 — a raising channel is a failed send
        logger.warning("Alert %s delivery via %s raised: %s", shown_id, name, exc)
        return False
    if not getattr(receipt, "complete", False):
        logger.warning(
            "Alert %s delivery via %s was not acknowledged (status=%s)",
            shown_id,
            name,
            getattr(receipt, "status", None),
        )
        return False
    return True


async def _send_telegram(level: str, title: str, body: str) -> bool:
    """Page via Telegram. True only when Telegram acknowledged the message.

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


def alert_webhook_url() -> str:
    """The alert webhook, vault first.

    Through the accessor because ``genus secrets status`` can only measure the
    accessor: it reported ``served=vault`` for this name while this reader
    looked only at the environment, so the runbook's verify step attested that
    the SOPS copy was safe to delete and the next restart would have lost
    alerting — the channel an operator learns everything else through.
    """
    from robothor.secrets import get_secret

    return get_secret("ROBOTHOR_ALERT_WEBHOOK_URL") or ""


async def _send_webhook(level: str, title: str, body: str, metadata: dict[str, Any] | None) -> bool:
    """Send alert via webhook (extensibility point for PagerDuty, Slack, etc.)."""
    webhook_url = alert_webhook_url()
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
