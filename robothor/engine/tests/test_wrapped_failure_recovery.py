"""Error recovery names the tool a ``tool_call`` wrapped, and counts per tool.

The loop recorded a failed call for error recovery under the name the model
used, so on a deferred run every failure was a failure of ``tool_call``. Three
authentication failures of three different wrapped tools read as three in a
row of one tool, and the run spawned "Diagnose authentication failure for tool
'tool_call'" helpers: two in one turn, each asked about a tool that does not
exist as a thing to diagnose. The circuit breaker already judges the wrapped
call; recovery now does too.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tests.test_tool_search_promotes import _broad, _drive
from robothor.engine.tools.registry import ToolRegistry

AUTH_ERROR = {"error": "401 Unauthorized: the access token has expired"}


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)


async def _helpers(wrapped: list[str], engine_config) -> list[str]:
    """Run a deferred agent whose wrapped calls all fail auth; return each helper's brief."""
    registry = ToolRegistry()
    cfg = _broad(registry)
    cfg.can_spawn_agents = True
    cfg.error_feedback = True  # the escalation manager that counts consecutive failures

    async def execute(name: str, args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(AUTH_ERROR)

    calls = [("tool_call", {"name": n, "arguments": {}}) for n in wrapped]
    spawn = AsyncMock(return_value="checked the credentials")
    with (
        patch.object(registry, "execute", execute),
        patch(
            "robothor.engine.run_lifecycle.RunLifecycleMixin._spawn_recovery_helper",
            spawn,
        ),
    ):
        await _drive(cfg, registry, calls, engine_config)
    return [str(c.kwargs["action"].message) for c in spawn.await_args_list]


class TestRecoveryJudgesTheWrappedTool:
    @pytest.mark.asyncio
    async def test_different_wrapped_tools_do_not_add_up(self, engine_config) -> None:
        briefs = await _helpers(
            ["gws_gmail_search", "gws_calendar_list", "jira_search"], engine_config
        )
        assert briefs == []

    @pytest.mark.asyncio
    async def test_one_wrapped_tool_failing_three_times_gets_one_helper(
        self, engine_config
    ) -> None:
        briefs = await _helpers(["jira_search"] * 3, engine_config)

        assert len(briefs) == 1, briefs
        assert "'jira_search'" in briefs[0]
        assert "'tool_call'" not in briefs[0]
