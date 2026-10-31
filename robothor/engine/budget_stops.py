"""Budgets that report: one record for every stop a budget, cap or control makes.

A budget that refuses, ends or degrades a run used to leave, at best, a free-text
ERROR step and a log line -- at worst a SKIPPED row nobody reads. An operator
looking at a run that ended early could not tell which limit did it, what the
limit was, or whether it would have fired at all had they never set it.

This module is that record. :func:`record` appends one ``budget_stop`` step to
the run (``tool_name`` is the stop, ``tool_input`` carries ``reason``,
``setting``, ``limit``, ``observed`` and ``mode``) and publishes the same fields
as ``agent.budget_stop`` on the telemetry stream, so the Runs view and the
metrics pipeline see the same thing. ``mode`` is ``observe`` when the budget
recorded what it WOULD have done and let the run continue, ``enforce`` when it
acted.

It also owns the rule for spend budgets whose limit the operator never set:
they observe. Setting the limit explicitly in the environment, or setting the
budget's ``*_MODE`` variable to ``enforce``, keeps the old behaviour. See
``docs/BUDGETS_AND_STOPS.md``.

Everything here is best-effort and never raises: a record that crashes the run
it describes is worse than no record.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

Mode = Literal["observe", "enforce"]

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


def spend_cap_mode(limit_var: str, mode_var: str) -> Mode:
    """``enforce`` only when the operator asked for it.

    The mode variable wins when it says ``observe`` or ``enforce``. Without it,
    a limit set explicitly in the environment enforces, and a limit that is
    only the built-in default observes.
    """
    raw = os.environ.get(mode_var, "").strip().lower()
    if raw in ("observe", "enforce"):
        return raw  # type: ignore[return-value]
    return "enforce" if os.environ.get(limit_var, "").strip() else "observe"


def hourly_cost_cap_mode() -> Mode:
    return spend_cap_mode("ROBOTHOR_HOURLY_COST_CAP_USD", "ROBOTHOR_HOURLY_COST_CAP_MODE")


def runaway_hard_cap_mode() -> Mode:
    return spend_cap_mode("ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS", "ROBOTHOR_RUNAWAY_HARD_CAP_MODE")


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
                f"observed {detail['observed']}. Set the limit explicitly or its mode to "
                "enforce to stop runs instead.",
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
    logger.warning("budget_stop [%s] %s run=%s: %s", mode, stop, session.run_id, reason)
    publish(session.run_id, session.run.agent_id, session.run.tenant_id or "", detail)
    return step


def record_for_run(run_id: str, agent_id: str, **fields: Any) -> None:
    """Write a ``budget_stop`` step straight to a run row that is not live
    (the SKIPPED row an enforcing admission writes). Never raises."""
    detail = _detail(**fields)
    try:
        from robothor.engine.tracking import create_step

        create_step(_step(run_id, 1, detail))
    except Exception:  # noqa: BLE001
        logger.debug("budget_stop step could not be written", exc_info=True)
    publish(run_id, agent_id, "", detail)


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


def runaway_cap_hit(session: Any, cap: int, used: int, reason: str) -> Mode:
    """Record the runaway-token hard cap and say whether it stops the run.

    A cap the operator never set observes: the step (and the caller's critical
    alert) once, and the run goes on. ``ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS`` set
    explicitly, or ``ROBOTHOR_RUNAWAY_HARD_CAP_MODE=enforce``, stops it.
    """
    mode = runaway_hard_cap_mode()
    record(
        session,
        stop=RUNAWAY_TOKEN_CAP,
        reason=reason,
        setting="ROBOTHOR_RUNAWAY_HARD_CAP_TOKENS",
        limit=cap,
        observed=used,
        mode=mode,
    )
    return mode


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


def _hourly_detail(pool: Any, verb: str, mode: str) -> dict[str, Any] | None:
    """The hourly cap's record, or None when the pool has not reached it."""
    over = _crossed(pool)
    if over is None:
        return None
    observed, cap = over
    return _detail(
        HOURLY_COST_CAP,
        f"Hourly cost cap {verb}: ${observed:.2f}/${cap:.2f}",
        "ROBOTHOR_HOURLY_COST_CAP_USD",
        cap,
        round(observed, 4),
        mode,
    )


def _crossed(pool: Any) -> tuple[float, float] | None:
    try:
        over = pool.cost_cap_crossed()
        return (float(over[0]), float(over[1])) if over else None
    except Exception:  # noqa: BLE001 - a pool without the probe has no record to make
        return None


def observe_hourly_cap(pool: Any, agent_id: str) -> bool:
    """Note an observing pool's cap crossing for the run about to start.

    Returns True: the run was admitted, and this only adds the record.
    """
    detail = _hourly_detail(pool, "crossed", "observe")
    if detail is not None:
        note_pending(agent_id, **detail)
    return True


def record_hourly_cap_refusal(
    pool: Any, run_id: str | None, agent_id: str, reason: str, admission_mode: str
) -> None:
    """The SKIPPED row an enforcing cap left behind carries the step.

    ``admission_mode`` decides the step's mode: at admission ``observe`` the
    refusal was only recorded and the run went ahead.
    """
    if not run_id or not reason.startswith("Hourly cost cap"):
        return
    mode = "enforce" if admission_mode == "enforce" else "observe"
    detail = _hourly_detail(pool, "reached", mode)
    if detail is not None:
        record_for_run(run_id, agent_id, **detail)
