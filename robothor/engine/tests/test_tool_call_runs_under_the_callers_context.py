"""A tool reached through ``tool_call`` runs under the caller's whole context.

On a deferred run ``tool_call`` is the normal path to every non-core tool, so
whatever the wrapper drops from the ``ToolContext`` the wrapped tool never
sees. It used to re-list the context fields by hand and leave ``identity``
out: every data-scoped read then computed its scope from ``None``, which is
the unrestricted one, and a member reached rows a direct call would have cut
to "own data + shared". The rule pinned here: the inner call receives a
``ToolContext`` equal, field by field, to the one a direct call of the same
tool would receive in that run.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools import dispatch
from robothor.engine.tools.dispatch import (
    ToolContext,
    clear_deferred_allowed,
    set_deferred_allowed,
)
from robothor.engine.tools.handlers.toolsearch import _tool_call
from robothor.engine.tools.registry import get_registry
from robothor.identity import IdentityContext

MEMBER = IdentityContext(
    tenant_id="tenant-a",
    channel="webchat",
    identifier="user-1",
    verified=True,
    role="member",
    person_id="11111111-1111-4111-8111-111111111111",
)

#: One value per ToolContext field, none of them the default, so a field the
#: wrapper drops shows up as a difference.
RUN_CONTEXT: dict[str, Any] = {
    "agent_id": "example-agent",
    "run_id": "run-1",
    "tenant_id": "tenant-a",
    "workspace": "/tmp/ws",
    "user_id": "user-1",
    "user_role": "member",
    "accessible_tenant_ids": ("tenant-a",),
    "task_author_override": "scout",
    "is_benchmark": True,
    "identity": MEMBER,
}


def test_the_fixture_sets_every_tool_context_field() -> None:
    assert set(RUN_CONTEXT) == {f.name for f in dataclasses.fields(ToolContext)}


async def _through_registry(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await get_registry().execute(name, args, timeout=0, **RUN_CONTEXT)


async def _direct_and_wrapped(name: str, args: dict[str, Any]) -> None:
    token = set_deferred_allowed(frozenset({name}))
    try:
        await _through_registry(name, args)
        await _through_registry("tool_call", {"name": name, "arguments": args})
    finally:
        clear_deferred_allowed(token)


@pytest.mark.asyncio
async def test_tool_call_hands_the_wrapped_tool_the_direct_calls_context() -> None:
    seen: list[ToolContext] = []

    async def _probe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        seen.append(ctx)
        return {"ok": True}

    handlers = {"probe": _probe, "tool_call": _tool_call}
    with (
        patch.object(dispatch, "_get_handlers", return_value=handlers),
        patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
        patch.object(dispatch, "_runtime_denial", AsyncMock(return_value=None)),
    ):
        await _direct_and_wrapped("probe", {})

    direct, wrapped = seen
    for f in dataclasses.fields(ToolContext):
        assert getattr(wrapped, f.name) == getattr(direct, f.name), f.name
    assert wrapped.identity is MEMBER


def _mode_env(mode: str) -> Any:
    return patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": mode}, clear=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "dal"), [("list_people", "list_people"), ("list_tasks", "list_tasks")]
)
async def test_a_scoped_read_through_tool_call_is_cut_like_the_direct_one(
    tool: str, dal: str
) -> None:
    with (
        _mode_env("enforce"),
        patch(f"robothor.crm.dal.{dal}", return_value=[]) as mock_dal,
        patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
        patch.object(dispatch, "_runtime_denial", AsyncMock(return_value=None)),
    ):
        await _direct_and_wrapped(tool, {})

    direct, wrapped = (c.kwargs.get("scope") for c in mock_dal.call_args_list)
    assert direct is not None and direct.restricted
    assert wrapped == direct


@pytest.mark.asyncio
async def test_spawn_agent_through_tool_call_carries_the_callers_identity() -> None:
    from robothor.engine.tools.handlers import spawn

    handler = AsyncMock(return_value={"status": "completed"})
    with (
        patch.object(spawn, "_handle_spawn_agent", handler),
        patch.object(dispatch, "_refuse_before_handler", AsyncMock(return_value=None)),
        patch.object(dispatch, "_runtime_denial", AsyncMock(return_value=None)),
    ):
        await _direct_and_wrapped("spawn_agent", {"agent_id": "worker", "message": "hi"})

    direct, wrapped = (c.args[1] for c in handler.await_args_list)
    assert direct.identity is MEMBER
    assert wrapped == direct
