"""A tool call over the per-minute limit waits for room instead of failing.

The guardrail engine's ``rate_limit`` policy refuses a call once an agent has
made its per-minute allowance. An agent run used to pass that refusal to the
model, which in an owner's chat turn meant three calls failing with "Rate
limit exceeded" and the agent then explaining a "policy gate" to a person who
had asked a question. A burst is what reading ten things looks like; an early
call is not a wrong one. Workflow steps have always waited in place for the
same limiter (``workflow._run_tool_step``), and this is the agent-run half.

The wait lives at the top of the guardrail gate
(``tool_admission._check_guardrails``), because every call an agent makes is
admitted there one at a time, after the cheaper gates have had their say: a call on its own turn, each call of a parallel batch (admitted in the
model's order before the batch runs), and each call a snippet makes through
``execute_code``.

What bounds it is the run's own deadline, never a new timeout. Three already
exist and the wait reads all of them: the host's runtime deadline
(``runtime.deadlines``), the stall watchdog's hard ceiling (the same number
the run's outer ``asyncio.timeout`` uses), and the budget stop parked on the
session once a run is wrapping up (``run_deadline.begin_wrapup``). When the
deadline would pass before the window has room, the call waits out what is
left and is then not run, with the same words a budget stop uses for a call it
never made; the run's loop then ends the run exactly as it ends any run at its
deadline. A cancellation from the outer timeout lands in the sleep like it
lands anywhere else.

The wait is declared to the stall watchdog as a bounded wait, so the idle
clock does not read a paced run as a wedged one.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from robothor.engine.guardrails import GuardrailEngine
from robothor.engine.run_deadline import WRAPUP_ATTR
from robothor.engine.stall_watchdog import _active_watchdog_var
from robothor.engine.tool_verdict import ToolVerdict

__all__ = ["NOT_RUN_AT_BUDGET", "seconds_left", "wait_for_room"]

#: The words a call the run never made is answered with at its deadline: the
#: same sentence ``run_deadline`` closes a dangling call with, because it is the
#: same event. Nothing in it names the limit the call was waiting on.
NOT_RUN_AT_BUDGET = "[SYSTEM] Not run: the run reached its wall-clock budget."


def seconds_left(session: Any, watchdog: Any) -> float | None:
    """The time before the nearest of the run's deadlines, or None for none."""
    from robothor.engine.runtime.deadlines import remaining

    limits: list[float] = []
    with contextlib.suppress(Exception):
        host = remaining()
        if host is not None:
            limits.append(float(host))
    hard = getattr(watchdog, "remaining_seconds", None)
    if isinstance(hard, int | float):
        limits.append(float(hard))
    stop = getattr(session, WRAPUP_ATTR, None)
    if stop is not None:
        with contextlib.suppress(Exception):
            if stop.budget.bounded:
                limits.append(float(stop.remaining()))
    return min(limits) if limits else None


async def _pause(seconds: float, watchdog: Any) -> None:
    if watchdog is not None:
        watchdog.begin_wait("tool_rate_limit", seconds)
    try:
        await asyncio.sleep(seconds)
    finally:
        if watchdog is not None:
            watchdog.end_wait()


async def wait_for_room(guardrail_engine: Any, agent_id: str, session: Any) -> ToolVerdict | None:
    """Wait until ``agent_id`` may make one more call. None = go ahead.

    A verdict is returned only when the run's deadline comes first: the call
    is then not run, and nothing in the verdict mentions the limit.
    """
    if not isinstance(guardrail_engine, GuardrailEngine):
        return None
    watchdog = _active_watchdog_var.get()
    while (wait := guardrail_engine.rate_limit_wait(agent_id)) > 0:
        left = seconds_left(session, watchdog)
        if left is not None and left <= wait:
            await _pause(max(0.0, left), watchdog)
            return ToolVerdict(
                allowed=False,
                message=NOT_RUN_AT_BUDGET,
                output={"guard": "run_wrapup"},
                count_as_iteration_error=False,
            )
        await _pause(wait, watchdog)
    return None
