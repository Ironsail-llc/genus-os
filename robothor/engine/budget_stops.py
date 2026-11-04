"""Budgets that report: one record for every stop a budget, cap or control makes.

A budget that refuses, ends or degrades a run used to leave, at best, a free-text
ERROR step and a log line -- at worst a SKIPPED row nobody reads. An operator
looking at a run that ended early could not tell which limit did it, what the
limit was, or whether it would have fired at all had they never set it.

This module is that record. :func:`record` appends one ``budget_stop`` step to
the run (``tool_name`` is the stop, ``tool_input`` carries ``reason``,
``setting``, ``limit``, ``observed`` and ``mode``) and publishes the same fields
as ``agent.budget_stop`` on the telemetry stream, so the Runs view and the
metrics pipeline see the same thing. ``mode`` is ``observe`` when a spend
budget's limit was reached and the run continued, ``enforce`` when a control
(the operator's stop, the iteration safety valve, an explicit per-agent cost
cap) acted.

Spend budgets always observe. The fleet hourly cost cap and the runaway-token
hard cap are thresholds for this record and its alert, never a reason to refuse
or end a run. See ``docs/BUDGETS_AND_STOPS.md``.

Everything here is best-effort and never raises: a record that crashes the run
it describes is worse than no record.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from datetime import UTC, datetime
from typing import Any

from robothor.sanitize import sanitize_log

logger = logging.getLogger(__name__)

TELEMETRY_STREAM = "telemetry"
EVENT_TYPE = "agent.budget_stop"

HOURLY_COST_CAP = "hourly_cost_cap"
RUNAWAY_TOKEN_CAP = "runaway_token_cap"

#: Stops noted before their run exists (admission happens before the run row
#: is created). Held in the task's context, so they reach exactly the run the
#: same task goes on to execute, and adopted by :func:`adopt_pending`.
_pending: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "budget_stops_pending", default=()
)


def runaway_cap_note() -> str:
    """What the runaway pages may honestly say about containment."""
    return "Not contained: the hard cap is recorded and the run continues"


def _detail(
    stop: str, reason: str, setting: str, limit: Any, observed: Any, mode: str
) -> dict[str, Any]:
    return {
        "stop": stop,
        "reason": reason,
        "setting": setting,
        "limit": limit,
        "observed": observed,
        "mode": mode,
    }


def _step(run_id: str, step_number: int, detail: dict[str, Any]) -> Any:
    from robothor.engine.models import RunStep, StepType

    now = datetime.now(UTC)
    return RunStep(
        run_id=run_id,
        step_number=step_number,
        step_type=StepType.BUDGET_STOP,
        tool_name=detail["stop"],
        tool_input=detail,
        started_at=now,
        completed_at=now,
    )


def _publish(payload: dict[str, Any]) -> None:
    try:
        from robothor.events import bus

        bus.publish(
            TELEMETRY_STREAM,
            EVENT_TYPE,
            payload,
            source="engine",
            tenant_id=str(payload.get("tenant_id") or ""),
        )
    except Exception:  # noqa: BLE001 - telemetry is never a verdict
        logger.debug("budget_stop telemetry publish failed", exc_info=True)


def publish(run_id: str, agent_id: str, tenant_id: str, detail: dict[str, Any]) -> None:
    """Put the stop on the telemetry stream, off the event loop when there is one."""
    payload = {**detail, "run_id": run_id, "agent_id": agent_id, "tenant_id": tenant_id}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _publish(payload)
        return
    loop.run_in_executor(None, _publish, payload)


#: Seconds between two observe alerts for the same stop: every scheduled run
#: past the hourly cap is recorded, but the operator hears about it once an hour.
ALERT_INTERVAL_S = 3600.0
_last_alert: dict[str, float] = {}


def _alert(detail: dict[str, Any], agent_id: str) -> None:
    """The existing alert path, for a budget that observed instead of stopping."""
    now = time.monotonic()
    last = _last_alert.get(detail["stop"])
    if last is not None and now - last < ALERT_INTERVAL_S:
        return
    _last_alert[detail["stop"]] = now
    try:
        from robothor.engine.alerts import alert
        from robothor.engine.task_registry import get_task_registry

        get_task_registry().spawn(
            alert(
                "warning",
                f"Budget observed, run continued: {detail['stop']} ({agent_id})",
                f"{detail['reason']} -- setting {detail['setting']}, limit {detail['limit']}, "
                f"observed {detail['observed']}.",
            ),
            name=f"budget-observed-alert:{detail['stop']}:{agent_id}",
        )
    except Exception:  # noqa: BLE001
        logger.debug("budget_stop alert dispatch failed", exc_info=True)


def record(
    session: Any,
    *,
    stop: str,
    reason: str,
    setting: str,
    limit: Any,
    observed: Any,
    mode: str,
) -> Any:
    """Append a ``budget_stop`` step to a live run and publish it. Never raises."""
    detail = _detail(stop, reason, setting, limit, observed, mode)
    try:
        session._step_counter += 1
        step = _step(session.run_id, session._step_counter, detail)
        session.run.steps.append(step)
    except Exception:  # noqa: BLE001
        logger.debug("budget_stop step could not be recorded", exc_info=True)
        return None
    logger.warning(
        "budget_stop [%s] %s run=%s: %s",
        sanitize_log(mode),
        sanitize_log(stop),
        sanitize_log(session.run_id),
        sanitize_log(reason),
    )
    publish(session.run_id, session.run.agent_id, session.run.tenant_id or "", detail)
    return step


def note_pending(agent_id: str, **fields: Any) -> None:
    """Hold an observed stop for the run this task is about to start, and alert."""
    detail = _detail(**fields)
    logger.warning(
        "budget_stop [observe] %s agent=%s: %s", detail["stop"], agent_id, detail["reason"]
    )
    _pending.set((*_pending.get(), detail))
    _alert(detail, agent_id)


def adopt_pending(session: Any) -> int:
    """Move held stops onto the run that has just been created."""
    held = _pending.get()
    if not held:
        return 0
    _pending.set(())
    for detail in held:
        record(session, **detail)
    return len(held)


DURABLE_STOP = "durable_stop"


def record_durable_stop(session: Any, reason: str) -> Any:
    """The operator's stop: a control, so always ``enforce``. Recorded once.

    Called where the stop is issued to a live session (``runtime.activity.
    stop_local``, ``controls.signal_stopped``) as well as where a later
    provider request is refused. A stop that lands during a model call cancels
    that call, so no later request ever reaches the refusal; recording at the
    issue point is what puts the step on that run. Never raises.
    """
    try:
        if any(
            getattr(step, "tool_name", None) == DURABLE_STOP
            and getattr(step, "step_type", None) == "budget_stop"
            for step in list(session.run.steps)
        ):
            return None
    except Exception:  # noqa: BLE001
        return None
    return record(
        session,
        stop=DURABLE_STOP,
        reason=reason,
        setting="agent_runtime_controls",
        limit=None,
        observed=None,
        mode="enforce",
    )


def runaway_cap_hit(session: Any, cap: int, used: int, reason: str) -> None:
    """Record the runaway-token hard cap. The caller pages; the run goes on."""
    record(
        session,
        stop=RUNAWAY_TOKEN_CAP,
        reason=reason,
        setting="ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS",
        limit=cap,
        observed=used,
        mode="observe",
    )


def safety_cap(cap: int, iteration: int) -> dict[str, Any]:
    """The iteration safety valve: a control, so it always enforces."""
    return _detail(
        "safety_cap",
        f"Safety limit reached ({cap} iterations).",
        "safety_cap",
        cap,
        iteration,
        "enforce",
    )


def cost_cap(limit: float, run: Any) -> dict[str, Any]:
    """The explicit per-agent cost cap (``v2.hard_budget`` + ``v2.max_cost_usd``)."""
    return _detail(
        "hard_budget",
        "Hard budget limit reached (explicit cost cap).",
        "v2.max_cost_usd",
        limit,
        round(float(getattr(run, "total_cost_usd", 0.0) or 0.0), 6),
        "enforce",
    )


def _hourly_detail(pool: Any) -> dict[str, Any] | None:
    """The hourly cap's record, or None when the pool has not reached it."""
    over = _crossed(pool)
    if over is None:
        return None
    observed, cap = over
    return _detail(
        HOURLY_COST_CAP,
        f"Hourly cost cap crossed: ${observed:.2f}/${cap:.2f}",
        "ROBOTHOR_HOURLY_COST_CAP_USD",
        cap,
        round(observed, 4),
        "observe",
    )


def _crossed(pool: Any) -> tuple[float, float] | None:
    try:
        over = pool.cost_cap_crossed()
        return (float(over[0]), float(over[1])) if over else None
    except Exception:  # noqa: BLE001 - a pool without the probe has no record to make
        return None


def observe_hourly_cap(pool: Any, agent_id: str) -> bool:
    """Note the pool's cap crossing for the run about to start.

    Returns True: the run was admitted, and this only adds the record.
    """
    detail = _hourly_detail(pool)
    if detail is not None:
        note_pending(agent_id, **detail)
    return True
