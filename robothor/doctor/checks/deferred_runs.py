"""Deferred runs: hook workflow runs the tool-call rate limit deferred.

The engine counts every hook workflow run it defers because the tool-call
rate limit would block its first step (``robothor.engine.deferrals``), per
workflow and per minute window, in Redis. This check sums the last hour and
warns past ``ROBOTHOR_DEFERRED_RUNS_WARN_THRESHOLD``: a deferred run's event
waits in the workflow's backlog (``robothor.engine.workflow_backlog``) and runs
in turn, so a high count means work is waiting, not lost. The detail line also
says what is waiting now, per workflow: the backlog's depth and the age of its
oldest event. A backlog whose oldest event has waited longer than the look-back
period fails the check (a ``warning``) whatever the count: that workflow is not
keeping up with what feeds it.

It alerts in two tiers, following :mod:`robothor.engine.alerts`, where only
``critical`` pages and ``warning`` becomes an alert-digest row: past the warn
threshold a ``warning`` alert, past ``ROBOTHOR_DEFERRED_RUNS_PAGE_THRESHOLD``
(never below the warn threshold) a ``critical`` one. ``critical`` goes to the
Telegram sender, which only the engine daemon registers, so from the doctor's
own process a page reaches a person only where an alert channel can deliver
outside the daemon; otherwise it is recorded as an undelivered alert and the
failing check is the signal. Each tier makes at most one attempt per look-back
period, counted from its last attempt, and an attempt that was not delivered is
retried after :data:`ALERT_RETRY_SECONDS`. The detail line says what happened
to the page.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip
from robothor.engine.deferrals import (
    LOOKBACK_SECONDS,
    alert_lock_key,
    process_tenant_id,
    recent_deferrals,
)
from robothor.engine.workflow_backlog import backlog_stats

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

logger = logging.getLogger(__name__)

__all__ = ["CHECKS", "LOOKBACK_SECONDS"]

#: How many workflows the warning names.
TOP_WORKFLOWS = 5


def _client(ctx: DoctorContext) -> Any:
    from robothor.doctor.checks.redis import client

    return client(ctx)


#: How long an attempt that was not delivered holds the lock before the next
#: run may try again. Ten minutes: a doctor run every minute writes at most six
#: undelivered-alert rows an hour instead of sixty, and a channel that was down
#: for a moment is retried well inside the hour rather than after it.
ALERT_RETRY_SECONDS = 600

#: What :func:`_alert_once` found, for the check's detail line.
DELIVERED = "delivered"
ALREADY_SENT = "already sent"
NOT_DELIVERED = "not delivered"
FAILED = "failed"

#: The lock's value once an attempt was not delivered.
_UNDELIVERED = "undelivered"


async def _alert_once(
    ctx: DoctorContext, client: Any, tenant_id: str, level: str, title: str, body: str
) -> str:
    """Send the alert at ``level``: at most one attempt per tenant and level a period.

    The tenant's lock for the level is claimed before sending, with the
    look-back period as its TTL, so two doctors never both send and the period
    runs from this attempt rather than from the clock hour. The lock is never
    released: an attempt that was not delivered keeps it with the shorter
    :data:`ALERT_RETRY_SECONDS` TTL, so the next attempt comes after that and
    not on every run. A warning already sent does not hold back a critical
    alert: each level has its own lock.

    Returns :data:`DELIVERED`; :data:`ALREADY_SENT` when a delivered alert
    holds the lock; :data:`NOT_DELIVERED` when this attempt, or one still
    holding the short lock, was not delivered; :data:`FAILED` when the lock
    could not be taken.
    """
    from robothor.engine import alerts

    lock = alert_lock_key(tenant_id, level)
    try:
        claimed = await ctx.run_blocking(
            lambda: client.set(lock, "sent", nx=True, ex=LOOKBACK_SECONDS)
        )
        if not claimed:
            held = await ctx.run_blocking(lambda: client.get(lock))
            held = held.decode() if isinstance(held, bytes) else held
            return NOT_DELIVERED if held == _UNDELIVERED else ALREADY_SENT
    except Exception as exc:  # noqa: BLE001 - the printed warning still stands
        logger.warning("doctor: could not send the deferred-runs alert: %s", exc)
        return FAILED
    delivered = False
    try:
        delivered = bool(await alerts.alert(level, title, body))
    except Exception as exc:  # noqa: BLE001 - the printed warning still stands
        logger.warning("doctor: could not send the deferred-runs alert: %s", exc)
    if delivered:
        return DELIVERED
    try:
        await ctx.run_blocking(lambda: client.set(lock, _UNDELIVERED, ex=ALERT_RETRY_SECONDS))
    except Exception as exc:  # noqa: BLE001 - the full-period lock still stands
        logger.warning("doctor: could not shorten the deferred-runs alert lock: %s", exc)
    return NOT_DELIVERED


def _page_note(outcome: str) -> str:
    """What happened to the page, for the detail line."""
    if outcome == DELIVERED:
        return "page sent"
    if outcome == ALREADY_SENT:
        return "page already sent this hour"
    if outcome == NOT_DELIVERED:
        return (
            "page not delivered from this process: no alert channel here can deliver "
            "it, so it is recorded as an undelivered alert (an alert_fallback "
            f"notification row); tried again after {ALERT_RETRY_SECONDS // 60} minutes"
        )
    return "page not sent: the alert lock could not be taken"


async def _recent(ctx: DoctorContext) -> Result:
    """Hook workflow runs the tool-call rate limit deferred in the last hour.

    A deferred run's event is requeued and runs later, so a count here means
    the instance is saturated: its hook workflows produce tool calls faster
    than their ``rate_limit_per_minute`` admits. Raise that limit on the
    workflows named (docs/AGENT_BUILDER.md, "Tool-call rate limit"), or slow
    what feeds their stream. The engine log has one summary line per workflow
    per minute: search it for ``deferred=``. Past the warn threshold the check
    also sends a warning alert (an alert-digest row); past the page threshold
    a critical alert, and the detail line says whether it was delivered.
    """
    threshold = int(ctx.settings.engine.deferred_runs_warn_threshold)
    page_threshold = max(threshold, int(ctx.settings.engine.deferred_runs_page_threshold))
    tenant_id = process_tenant_id()
    try:
        client = _client(ctx)
    except Exception as exc:  # noqa: BLE001 - unreachable is a skip, not a verdict
        return skip(f"could not read the deferral counts: {type(exc).__name__}")
    try:
        return await _verdict(ctx, client, tenant_id, threshold, page_threshold)
    finally:
        with contextlib.suppress(Exception):  # closing is best effort
            client.close()


async def _verdict(
    ctx: DoctorContext, client: Any, tenant_id: str, threshold: int, page_threshold: int
) -> Result:
    try:
        counts = await ctx.run_blocking(lambda: recent_deferrals(client, tenant_id))
    except Exception as exc:  # noqa: BLE001 - unreachable is a skip, not a verdict
        return skip(f"could not read the deferral counts: {type(exc).__name__}")

    try:
        waiting: dict[str, tuple[int, float]] | None = await ctx.run_blocking(
            lambda: backlog_stats(client, tenant_id)
        )
    except Exception as exc:  # noqa: BLE001 - the deferral count still stands
        logger.warning("doctor: could not read the workflow backlogs: %s", exc)
        waiting = None
    backlog = _backlog_note(waiting)
    stale = sorted(
        name for name, (_, oldest) in (waiting or {}).items() if oldest > LOOKBACK_SECONDS
    )

    total = sum(counts.values())
    if total <= threshold and not stale:
        return ok(f"{total} deferred in the last hour (threshold {threshold}); {backlog}")
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_WORKFLOWS]
    named = ", ".join(f"{name}: {count}" for name, count in top) or "none"
    detail = (
        f"{total} hook workflow runs deferred by the tool-call rate limit in the last "
        f"hour (threshold {threshold}) -- {named}; {backlog}"
    )
    if stale:
        detail += f"; waiting longer than {LOOKBACK_SECONDS // 60} minutes: {', '.join(stale)}"
    level = "critical" if total > page_threshold else "warning"
    if level == "critical":
        detail += f" (past the page threshold {page_threshold})"
    if not ctx.dry_run:
        outcome = await _alert_once(
            ctx, client, tenant_id, level, "Workflow runs deferred by the rate limit", detail
        )
        if level == "critical":
            detail += f"; {_page_note(outcome)}"
    return fail(detail)


def _backlog_note(waiting: dict[str, tuple[int, float]] | None) -> str:
    """What waits in the workflow backlogs now, deepest first, for the detail line."""
    if waiting is None:
        return "backlog unreadable"
    if not waiting:
        return "nothing waiting in the workflow backlogs"
    deepest = sorted(waiting.items(), key=lambda item: (-item[1][0], item[0]))[:TOP_WORKFLOWS]
    named = ", ".join(
        f"{name}: {depth} waiting, oldest {int(oldest)}s" for name, (depth, oldest) in deepest
    )
    return f"backlog -- {named}"


CHECKS: tuple[Check, ...] = (
    Check(
        id="deferred_runs.recent",
        title="Workflow runs deferred by the tool-call rate limit",
        category="deferred_runs",
        severity="recommended",
        run=_recent,
    ),
)
