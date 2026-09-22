"""Lifetime of host capabilities published for one native tool turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from robothor.engine.tool_turn import ToolTurnRequest
    from robothor.goals.report_channel import ReportTurn

from contextlib import contextmanager

from robothor.engine.goal_report_delivery import report_scope
from robothor.engine.tool_proxy import (
    RunToolProxy,
    clear_tool_proxy,
    proxy_allow_set,
    set_tool_proxy,
)


@contextmanager
def tool_turn_context(
    runner: Any, req: ToolTurnRequest, names: list[str], *, max_calls: int, max_approvals: int
) -> Iterator[ReportTurn]:
    token = set_tool_proxy(
        RunToolProxy(
            runner=runner,
            req=req,
            allowed=proxy_allow_set(req, runner.registry),
            max_calls=max_calls,
            max_approvals=max_approvals,
        )
    )
    try:
        with report_scope(req, names) as report_state:
            yield report_state
    finally:
        clear_tool_proxy(token)
