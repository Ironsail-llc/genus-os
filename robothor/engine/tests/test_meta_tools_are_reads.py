"""``tool_search`` and ``tool_describe`` are reads, and are treated as reads.

With deferral the only path over the threshold, a broad agent reaches most of
its tools through a search, and every search went through effect admission as
if it were a write: one durable reservation per call, and the call refused
outright when that database could not be reached. That is a per-turn cost on
the path this change exists to make cheaper, and a failure mode it added. Both
meta-tools change nothing, so they are read-only: no reservation, and they are
offered in plan mode like every other read. ``tool_call`` is not a read of its
own; it is judged as the call it wraps.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.tests.test_tool_search_promotes import (
    _broad,
    _by_name,
    _drive,
    _search_result,
)
from robothor.engine.tools.constants import READONLY_TOOLS
from robothor.engine.tools.registry import ToolRegistry
from robothor.engine.wrapped_call import is_read_only_call


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROBOTHOR_DEFERRED_TOOLS_THRESHOLD", "ROBOTHOR_DEFERRED_TOOLS_RECENT_RUNS"):
        monkeypatch.delenv(name, raising=False)


class TestNoEffectReservation:
    @pytest.mark.asyncio
    async def test_a_search_reserves_nothing_and_works_without_the_effect_store(
        self, engine_config
    ) -> None:
        """The effect store is unreachable here (conftest); a read must not need it."""
        registry = ToolRegistry()
        reserve = MagicMock(side_effect=AssertionError("a read was journalled"))
        with patch("robothor.engine.runtime.effects.begin", reserve):
            _seen, results = await _drive(
                _broad(registry),
                registry,
                [
                    ("tool_search", {"query": "search my email"}),
                    ("tool_describe", {"name": "gws_calendar_list"}),
                ],
                engine_config,
                bypass_effects=False,
            )

        reserve.assert_not_called()
        found = _search_result(results)
        assert found["results"], found
        described = next(r for r in results if r.get("name") == "gws_calendar_list")
        assert described["parameters"]["properties"], described
        assert not any("admission" in str(r.get("error", "")) for r in results), results


class TestPlanMode:
    def test_plan_mode_offers_the_search_and_describe_tools(self) -> None:
        registry = ToolRegistry()
        names = set(_by_name(registry.build_readonly_for_agent(_broad(registry))))
        assert {"tool_search", "tool_describe"} <= names
        assert "tool_call" not in names

    @pytest.mark.asyncio
    async def test_a_plan_mode_search_finds_and_promotes_only_reads(self, engine_config) -> None:
        registry = ToolRegistry()
        seen, results = await _drive(
            _broad(registry),
            registry,
            [("tool_search", {"query": "send an email to a person"})],
            engine_config,
            bypass_effects=False,
            readonly_mode=True,
        )

        found = _search_result(results)
        assert found["results"], found
        assert all(hit["name"] in READONLY_TOOLS for hit in found["results"]), found
        assert not found.get("promoted")
        assert set(_by_name(seen[-1])) == set(_by_name(seen[0]))


class TestToolCallIsJudgedByWhatItWraps:
    def test_the_wrapper_is_not_a_read_of_its_own(self) -> None:
        assert {"tool_search", "tool_describe"} <= READONLY_TOOLS
        assert "tool_call" not in READONLY_TOOLS
        assert is_read_only_call("tool_call", {"name": "read_file", "arguments": {}})
        assert not is_read_only_call("tool_call", {"name": "exec", "arguments": {}})
