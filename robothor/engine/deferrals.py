"""Hook workflow runs deferred by the tool-call rate limit: counted and summarised.

When the rate limit would block a hook-triggered run's first step, the run is
deferred and its event requeued (``WorkflowEngine._admit_or_defer`` and
``EventHooks._run_workflow``). A burst can defer thousands of runs, so instead
of one log line per deferral each one is:

* counted in Redis, in a hash per minute window keyed by workflow id, so the
  doctor (``deferred_runs.recent``) can read it from its own process; and
* tallied in memory, so that when a window closes one summary line per
  workflow is logged::

      Workflow runs deferred by the tool-call rate limit: workflow=<id> deferred=<n> window=<start>

Windows are wall-clock minutes (UTC), the same length as the limiter's own
per-minute window, so counts from several engines and the doctor agree on which
window is which. This module changes what is visible, never what is scheduled:
a counter that cannot be written is logged and the event is requeued anyway.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ALERT_LOCK_PREFIX",
    "KEY_PREFIX",
    "LOOKBACK_SECONDS",
    "WINDOW_SECONDS",
    "DeferredRuns",
    "alert_lock_key",
    "process_tenant_id",
    "recent_deferrals",
    "window_key",
]

#: One counting window: a wall-clock minute, the rate limiter's window length.
WINDOW_SECONDS = 60

#: How far back the doctor sums deferrals.
LOOKBACK_SECONDS = 3600

#: Redis key prefix. The tenant and then the window's start (epoch seconds)
#: complete the key: several engines, one per tenant, may share one Redis.
KEY_PREFIX = "robothor:workflow:deferred:"

#: The doctor's hourly alert lock, per tenant; the tenant and the hour follow.
ALERT_LOCK_PREFIX = "robothor:workflow:deferred-alerted:"

#: Each window's hash outlives the doctor's look-back, then expires.
KEY_TTL_SECONDS = 2 * LOOKBACK_SECONDS


def window_start(at: float) -> int:
    """The start (epoch seconds) of the window ``at`` falls in."""
    whole = int(at)
    return whole - whole % WINDOW_SECONDS


def window_key(tenant_id: str, start: int) -> str:
    """The Redis hash holding one tenant's counts for one window, by workflow id."""
    return f"{KEY_PREFIX}{tenant_id}:{start}"


def alert_lock_key(tenant_id: str, period: int) -> str:
    """The key that says one tenant's deferral alert went out in one look-back period."""
    return f"{ALERT_LOCK_PREFIX}{tenant_id}:{period}"


def process_tenant_id() -> str:
    """The tenant this process counts and reads as, resolved as the engine does:
    ``ROBOTHOR_TENANT_ID``, else ``ROBOTHOR_DEFAULT_TENANT``, else the default."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import process_tenant

    return process_tenant() or DEFAULT_TENANT


def _label(start: int) -> str:
    return datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeferredRuns:
    """Counts deferrals per window and logs one summary line per closed window."""

    def __init__(self, tenant_id: str, clock: Callable[[], float] = time.time) -> None:
        self.tenant_id = tenant_id
        self._clock = clock
        self._open: dict[int, Counter[str]] = {}

    async def record(self, workflow_id: str, redis_client: Any) -> None:
        """Count one deferral of ``workflow_id`` in the current window."""
        start = window_start(self._clock())
        self._summarise_before(start)
        self._open.setdefault(start, Counter())[workflow_id] += 1
        key = window_key(self.tenant_id, start)
        try:
            await redis_client.hincrby(key, workflow_id, 1)
            await redis_client.expire(key, KEY_TTL_SECONDS)
        except Exception as e:
            logger.warning("Could not count a deferred run of %s: %s", workflow_id, e)

    def flush(self) -> None:
        """Log the summary of every window that has closed. Cheap; call it often."""
        self._summarise_before(window_start(self._clock()))

    def _summarise_before(self, current: int) -> None:
        for start in sorted(s for s in self._open if s < current):
            for workflow_id, count in sorted(self._open.pop(start).items()):
                logger.warning(
                    "Workflow runs deferred by the tool-call rate limit: "
                    "workflow=%s deferred=%d window=%s",
                    workflow_id,
                    count,
                    _label(start),
                )


def recent_deferrals(
    client: Any, tenant_id: str, *, now: float | None = None, lookback: int = LOOKBACK_SECONDS
) -> dict[str, int]:
    """One tenant's deferrals by workflow over the last ``lookback`` seconds. Reads only.

    ``client`` is a synchronous Redis client. The window ``now`` falls in is
    included, so a burst still in progress is counted.
    """
    current = window_start(time.time() if now is None else now)
    totals: Counter[str] = Counter()
    for start in range(current - lookback + WINDOW_SECONDS, current + 1, WINDOW_SECONDS):
        for field, value in (client.hgetall(window_key(tenant_id, start)) or {}).items():
            name = field.decode() if isinstance(field, bytes) else str(field)
            totals[name] += int(value)
    return dict(totals)
