"""Deferred runs: hook workflow runs the tool-call rate limit deferred.

The engine counts every hook workflow run it defers because the tool-call
rate limit would block its first step (``robothor.engine.deferrals``), per
workflow and per minute window, in Redis. This check sums the last hour and
warns past ``ROBOTHOR_DEFERRED_RUNS_WARN_THRESHOLD``: a deferred run's event
is requeued and retried, so a high count means work is waiting, not lost --
until the requeue budget runs out and events reach the dead-letter stream.

It alerts in two tiers, following :mod:`robothor.engine.alerts`, where only
``critical`` pages and ``warning`` becomes an alert-digest row: past the warn
threshold a ``warning`` alert, past ``ROBOTHOR_DEFERRED_RUNS_PAGE_THRESHOLD``
(never below the warn threshold) a ``critical`` one. Each tier alerts at most
once per look-back period, counted from its last alert.
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

logger = logging.getLogger(__name__)

__all__ = ["CHECKS", "LOOKBACK_SECONDS"]

#: How many workflows the warning names.
TOP_WORKFLOWS = 5


def _client(ctx: DoctorContext) -> Any:
    from robothor.doctor.checks.redis import client

    return client(ctx)


async def _alert_once(
    ctx: DoctorContext, client: Any, tenant_id: str, level: str, title: str, body: str
) -> None:
    """Send the alert at ``level``, once per tenant and level per look-back period.

    The tenant's lock for the level is claimed before sending, with the period
    as its TTL, so two doctors never both send and the period runs from this
    alert rather than from the clock hour. It is released again when the alert
    was not delivered, so the next run tries again instead of waiting it out.
    A warning already sent does not hold back a critical alert: each level has
    its own lock.
    """
    from robothor.engine import alerts

    lock = alert_lock_key(tenant_id, level)
    try:
        claimed = await ctx.run_blocking(
            lambda: client.set(lock, "1", nx=True, ex=LOOKBACK_SECONDS)
        )
    except Exception as exc:  # noqa: BLE001 - the printed warning still stands
        logger.warning("doctor: could not send the deferred-runs alert: %s", exc)
        return
    if not claimed:
        return
    delivered = False
    try:
        delivered = bool(await alerts.alert(level, title, body))
    except Exception as exc:  # noqa: BLE001 - the printed warning still stands
        logger.warning("doctor: could not send the deferred-runs alert: %s", exc)
    if not delivered:
        try:
            await ctx.run_blocking(lambda: client.delete(lock))
        except Exception as exc:  # noqa: BLE001 - the lock expires on its own
            logger.warning("doctor: could not release the deferred-runs alert lock: %s", exc)


async def _recent(ctx: DoctorContext) -> Result:
    """Hook workflow runs the tool-call rate limit deferred in the last hour.

    A deferred run's event is requeued and runs later, so a count here means
    the instance is saturated: its hook workflows produce tool calls faster
    than their ``rate_limit_per_minute`` admits. Raise that limit on the
    workflows named (docs/AGENT_BUILDER.md, "Tool-call rate limit"), or slow
    what feeds their stream. The engine log has one summary line per workflow
    per minute: search it for ``deferred=``. Past the warn threshold the check
    also sends a warning alert (an alert-digest row); past the page threshold
    a critical alert, which pages. Each at most once an hour.
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

    total = sum(counts.values())
    if total <= threshold:
        return ok(f"{total} deferred in the last hour (threshold {threshold})")
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_WORKFLOWS]
    named = ", ".join(f"{name}: {count}" for name, count in top)
    detail = (
        f"{total} hook workflow runs deferred by the tool-call rate limit in the last "
        f"hour (threshold {threshold}) -- {named}"
    )
    level = "critical" if total > page_threshold else "warning"
    if level == "critical":
        detail += f" (past the page threshold {page_threshold})"
    if not ctx.dry_run:
        await _alert_once(
            ctx, client, tenant_id, level, "Workflow runs deferred by the rate limit", detail
        )
    return fail(detail)


CHECKS: tuple[Check, ...] = (
    Check(
        id="deferred_runs.recent",
        title="Workflow runs deferred by the tool-call rate limit",
        category="deferred_runs",
        severity="recommended",
        run=_recent,
    ),
)
