"""Deferred runs: hook workflow runs the tool-call rate limit deferred.

The engine counts every hook workflow run it defers because the tool-call
rate limit would block its first step (``robothor.engine.deferrals``), per
workflow and per minute window, in Redis. This check sums the last hour and
warns past ``ROBOTHOR_DEFERRED_RUNS_WARN_THRESHOLD``: a deferred run's event
is requeued and retried, so a high count means work is waiting, not lost --
until the requeue budget runs out and events reach the dead-letter stream.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip
from robothor.engine.deferrals import KEY_PREFIX, LOOKBACK_SECONDS, recent_deferrals

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

logger = logging.getLogger(__name__)

__all__ = ["CHECKS", "LOOKBACK_SECONDS"]

#: How many workflows the warning names.
TOP_WORKFLOWS = 5

#: Set when the warning has been sent for one look-back period, so a doctor
#: run every few minutes alerts once an hour, not every run.
ALERTED_KEY_PREFIX = f"{KEY_PREFIX}alerted:"


def _client(ctx: DoctorContext) -> Any:
    import redis as redis_lib

    settings = ctx.settings.redis
    return redis_lib.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password or None,
        socket_timeout=ctx.timeout_s,
        socket_connect_timeout=ctx.timeout_s,
    )


async def _alert_once(ctx: DoctorContext, client: Any, title: str, body: str) -> None:
    """Send the warning to the alert channel, once per look-back period."""
    from robothor.engine import alerts

    period = int(time.time()) // LOOKBACK_SECONDS
    try:
        first = await ctx.run_blocking(
            lambda: client.set(f"{ALERTED_KEY_PREFIX}{period}", "1", nx=True, ex=LOOKBACK_SECONDS)
        )
        if first:
            await alerts.alert("warning", title, body)
    except Exception as exc:  # noqa: BLE001 - the printed warning still stands
        logger.warning("doctor: could not send the deferred-runs alert: %s", exc)


async def _recent(ctx: DoctorContext) -> Result:
    """Hook workflow runs the tool-call rate limit deferred in the last hour.

    A deferred run's event is requeued and runs later, so a count here means
    the instance is saturated: its hook workflows produce tool calls faster
    than their ``rate_limit_per_minute`` admits. Raise that limit on the
    workflows named (docs/AGENT_BUILDER.md, "Tool-call rate limit"), or slow
    what feeds their stream. The engine log has one summary line per workflow
    per minute: search it for ``deferred=``. Past the threshold the warning is
    also sent to the alert channel, once an hour.
    """
    threshold = int(ctx.settings.engine.deferred_runs_warn_threshold)
    try:
        client = _client(ctx)
        counts = await ctx.run_blocking(lambda: recent_deferrals(client))
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
    if not ctx.dry_run:
        await _alert_once(ctx, client, "Workflow runs deferred by the rate limit", detail)
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
