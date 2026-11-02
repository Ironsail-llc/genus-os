"""The role capability tier travels with a call into the tool it reaches.

The capability tier is a ``ToolContext`` field: dispatch fills it from the
run's effective tier (``capability_tier.current_run_tier``), and the
handlers that widen or narrow on it read it from there. A handler that runs
another tool on its caller's behalf (``tool_call``) hands that call its whole
context through ``ToolContext.call_kwargs()``, so ``ToolRegistry.execute``
takes every field, the tier included. An explicit tier is the one the call
runs under; with none, dispatch reads the run's, as before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.capability_tier import clear_run_tier, set_run_tier
from robothor.engine.tools import dispatch
from robothor.engine.tools.dispatch import (
    ToolContext,
    clear_deferred_allowed,
    set_deferred_allowed,
)
from robothor.engine.tools.handlers.toolsearch import _tool_call
from robothor.engine.tools.registry import get_registry


def _probe_handlers(seen: list[ToolContext]) -> dict[str, Any]:
    async def _probe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        seen.append(ctx)
        return {"ok": True}

    return {"probe": _probe, "tool_call": _tool_call}


async def _run(name: str, args: dict[str, Any], seen: list[ToolContext], **kw: Any) -> None:
    with (
        patch.object(dispatch, "_get_handlers", return_value=_probe_handlers(seen)),
        patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
        patch.object(dispatch, "_runtime_denial", AsyncMock(return_value=None)),
    ):
        await get_registry().execute(name, args, timeout=0, agent_id="example-agent", **kw)


@pytest.mark.asyncio
async def test_an_explicit_tier_is_the_one_the_call_runs_under() -> None:
    seen: list[ToolContext] = []
    await _run("probe", {}, seen, capability_tier="unrestricted")
    assert [c.capability_tier for c in seen] == ["unrestricted"]


@pytest.mark.asyncio
async def test_with_no_tier_given_the_runs_effective_tier_is_used() -> None:
    seen: list[ToolContext] = []
    token = set_run_tier("elevated")
    try:
        await _run("probe", {}, seen)
    finally:
        clear_run_tier(token)
    assert [c.capability_tier for c in seen] == ["elevated"]


@pytest.mark.asyncio
async def test_a_wrapped_call_runs_under_the_callers_tier() -> None:
    seen: list[ToolContext] = []
    token = set_deferred_allowed(frozenset({"probe"}))
    try:
        await _run(
            "tool_call", {"name": "probe", "arguments": {}}, seen, capability_tier="unrestricted"
        )
    finally:
        clear_deferred_allowed(token)
    assert [c.capability_tier for c in seen] == ["unrestricted"]
