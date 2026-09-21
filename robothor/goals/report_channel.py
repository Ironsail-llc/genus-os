"""A run-scoped host channel for factual reports, separate from tool-result data.

The caller must enable this only for an admitted, standalone interactive report
tool. This module does not install that integration or decide request intent.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class ReportTurn:
    tenant_id: str
    agent_id: str
    run_id: str
    enabled: bool
    tool_name: str = "report_pursuit_goal"
    active: bool = True
    consumed: bool = False
    message: str | None = None
    finalizes: bool = True
    allowed_goal_statuses: tuple[str, ...] | None = None


_turn: ContextVar[ReportTurn | None] = ContextVar("goal_report_turn", default=None)


@contextmanager
def report_turn(
    tenant_id: str,
    agent_id: str,
    run_id: str,
    *,
    enabled: bool,
    tool_name="report_pursuit_goal",
    finalizes=True,
    allowed_goal_statuses=None,
):
    state = ReportTurn(tenant_id, agent_id, run_id, enabled, tool_name)
    state.finalizes = finalizes
    state.allowed_goal_statuses = allowed_goal_statuses
    token = _turn.set(state)
    try:
        yield state
    except BaseException:
        state.message = None
        raise
    finally:
        state.active = False
        _turn.reset(token)


def require_report_context(ctx, *, tool_name="report_pursuit_goal") -> ReportTurn:
    state = _turn.get()
    if (
        state is None
        or not state.active
        or not state.enabled
        or state.tool_name != tool_name
        or (state.tenant_id, state.agent_id, state.run_id)
        != (ctx.tenant_id, ctx.agent_id, ctx.run_id)
    ):
        raise ValueError("Goal reporting is unavailable in this execution context")
    return state


def publish_report(ctx, message: str, *, tool_name="report_pursuit_goal") -> None:
    state = require_report_context(ctx, tool_name=tool_name)
    if state.message is not None:
        raise ValueError("Only one final goal report is allowed per tool turn")
    if not isinstance(message, str) or not message.strip() or len(message) > 16000:
        raise ValueError("Invalid goal report")
    state.message = message


def consume_report(state: ReportTurn, run) -> str | None:
    if state.active:
        raise ValueError("The tool turn must finish before consuming its report")
    if (state.tenant_id, state.agent_id, state.run_id) != (run.tenant_id, run.agent_id, run.id):
        raise ValueError("Goal report belongs to another run")
    if state.consumed:
        return None
    state.consumed = True
    return state.message
