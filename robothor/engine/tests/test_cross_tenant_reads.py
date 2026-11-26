"""A ``tenant`` argument lets an allowlisted read run in a child tenant.

One mechanism, in dispatch, rather than an edit to every handler: when a call
carries ``tenant``, dispatch requires the tool to be on ``CROSS_TENANT_READS``
(or ``CROSS_TENANT_WRITES``) and the target to be reachable by this run, then
strips the argument and runs the handler inside ``tenant_scope(target)`` with a
context whose ``tenant_id`` is the target. Anything else is refused with one
sentence and runs nothing. With no ``tenant`` argument nothing changes.

The handler is a fake that records what it saw — its arguments, its context's
tenant, and the database scope both on the loop and inside
``asyncio.to_thread``, which is where every real handler checks out its
connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.db.connection import current_tenant_scope
from robothor.engine.models import TriggerType

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
OWNER_REACH = (PARENT, CHILD_A, CHILD_B)


class _Seen:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handler(self, result: dict[str, Any] | None = None):
        async def _h(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            in_thread = await asyncio.to_thread(current_tenant_scope)
            self.calls.append(
                {
                    "args": dict(args),
                    "tenant_id": ctx.tenant_id,
                    "accessible": ctx.accessible_tenant_ids,
                    "scope": current_tenant_scope(),
                    "scope_in_thread": in_thread,
                }
            )
            return dict(result or {"ok": True})

        return _h


def _session(**run: Any) -> SimpleNamespace:
    base = {
        "parent_run_id": None,
        "nesting_depth": 0,
        "trigger_type": TriggerType.WEBCHAT,
        "is_benchmark": False,
    }
    base.update(run)
    return SimpleNamespace(run=SimpleNamespace(**base))


@pytest.fixture
def env():
    seen = _Seen()
    handlers = {
        "list_tasks": seen.handler(),
        "write_file": seen.handler(),
        "get_task": seen.handler(),
    }
    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    state = SimpleNamespace(seen=seen, handlers=handlers, session=_session())
    with (
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=handlers),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch(
            "robothor.engine.session_registry.lookup",
            side_effect=lambda run_id: state.session,
        ),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        state.audit = audit
        yield state


def _call(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _execute_tool

    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": "alice",
        "user_role": "owner",
        "accessible_tenant_ids": OWNER_REACH,
    }
    base.update(kw)
    return asyncio.run(_execute_tool(name, args, **base))


# ── No tenant argument: exactly as before ──────────────────────────────────


def test_without_a_tenant_argument_nothing_changes(env):
    _call("list_tasks", {"status": "TODO"})

    (call,) = env.seen.calls
    assert call["args"] == {"status": "TODO"}
    assert call["tenant_id"] == PARENT
    assert call["accessible"] == OWNER_REACH
    assert call["scope"] is None
    env.audit.assert_not_called()


def test_an_empty_tenant_argument_is_no_tenant_argument(env):
    _call("list_tasks", {"tenant": "", "status": "TODO"})

    (call,) = env.seen.calls
    assert call["args"] == {"status": "TODO"}
    assert call["tenant_id"] == PARENT


# ── An allowed cross-tenant read ───────────────────────────────────────────


def test_owner_reads_a_child_in_the_childs_scope(env):
    result = _call("list_tasks", {"tenant": CHILD_A, "status": "TODO"})

    assert "error" not in result
    (call,) = env.seen.calls
    assert call["args"] == {"status": "TODO"}, "the tenant argument must be stripped"
    assert call["tenant_id"] == CHILD_A
    assert call["accessible"] == (CHILD_A,)
    assert call["scope"] == CHILD_A
    assert call["scope_in_thread"] == CHILD_A, "to_thread must carry the scope"
    # The scope ends with the call.
    assert current_tenant_scope() is None


def test_an_allowed_cross_tenant_read_is_audited(env):
    _call("list_tasks", {"tenant": CHILD_A})

    env.audit.assert_called_once()
    kw = env.audit.call_args.kwargs
    assert kw["home_tenant"] == PARENT
    assert kw["target_tenant"] == CHILD_A
    assert kw["user_id"] == "alice"
    assert kw["tool"] == "list_tasks"
    assert kw["outcome"] == "ok"


def test_naming_the_own_tenant_runs_as_an_ordinary_call(env):
    _call("list_tasks", {"tenant": PARENT})

    (call,) = env.seen.calls
    assert call["args"] == {}
    assert call["tenant_id"] == PARENT
    assert call["scope"] is None


# ── Refusals ───────────────────────────────────────────────────────────────


def _refused(result: dict[str, Any], env: Any) -> str:
    assert "error" in result, result
    assert result.get("refused") is True
    assert env.seen.calls == [], "a refused call must run nothing"
    return str(result["error"])


def test_a_member_is_refused(env):
    result = _call(
        "list_tasks", {"tenant": CHILD_A}, user_role="member", accessible_tenant_ids=(PARENT,)
    )
    _refused(result, env)


def test_unknown_and_unauthorised_tenants_get_the_same_refusal(env):
    unauthorised = _call(
        "list_tasks", {"tenant": CHILD_A}, user_role="member", accessible_tenant_ids=(PARENT,)
    )
    unknown = _call("list_tasks", {"tenant": "acme-nowhere"})

    assert _refused(unauthorised, env) == _refused(unknown, env)
    assert unauthorised["unlock"] == unknown["unlock"]
    assert unauthorised["gate_name"] == unknown["gate_name"]


def test_owner_of_a_child_cannot_reach_a_sibling_or_the_parent(env):
    as_child = {"tenant_id": CHILD_A, "accessible_tenant_ids": (CHILD_A,)}
    sibling = _call("list_tasks", {"tenant": CHILD_B}, **as_child)
    parent = _call("list_tasks", {"tenant": PARENT}, **as_child)
    unknown = _call("list_tasks", {"tenant": "acme-nowhere"}, **as_child)

    assert _refused(sibling, env) == _refused(parent, env) == _refused(unknown, env)


def test_a_sub_agent_is_refused_even_with_reach_in_its_context(env):
    env.session = _session(parent_run_id="parent-run", nesting_depth=1)
    _refused(_call("list_tasks", {"tenant": CHILD_A}), env)


def test_a_sub_agent_of_an_untracked_parent_is_refused(env):
    env.session = _session(trigger_type=TriggerType.SUB_AGENT, nesting_depth=1)
    _refused(_call("list_tasks", {"tenant": CHILD_A}), env)


def test_a_benchmark_run_is_refused(env):
    env.session = _session(is_benchmark=True)
    _refused(_call("list_tasks", {"tenant": CHILD_A}, is_benchmark=True), env)


def test_a_call_outside_a_live_run_is_refused(env):
    env.session = None
    _refused(_call("list_tasks", {"tenant": CHILD_A}), env)


def test_a_tool_off_the_allowlist_is_refused(env):
    _refused(_call("write_file", {"tenant": CHILD_A, "path": "x"}), env)


def test_a_tool_off_the_allowlist_is_refused_even_naming_its_own_tenant(env):
    _refused(_call("write_file", {"tenant": PARENT, "path": "x"}), env)


def test_a_refusal_is_audited_as_denied(env):
    _call("list_tasks", {"tenant": "acme-nowhere"})

    env.audit.assert_called_once()
    kw = env.audit.call_args.kwargs
    assert kw["target_tenant"] == "acme-nowhere"
    assert kw["outcome"] == "denied"


# ── as_of and ageDays on list results ─────────────────────────────────────


def test_list_results_gain_as_of_and_an_age_per_row(env):
    created = (datetime.now(UTC) - timedelta(days=3, hours=1)).isoformat()
    env.handlers["list_tasks"] = env.seen.handler(
        {"tasks": [{"id": "t1", "createdAt": created}, {"id": "t2", "createdAt": None}]}
    )

    result = _call("list_tasks", {"tenant": CHILD_A})

    assert "as_of" in result
    datetime.fromisoformat(result["as_of"])
    assert result["tasks"][0]["ageDays"] == 3
    assert result["tasks"][0]["createdAt"] == created, "existing fields are kept"
    assert result["tasks"][1]["ageDays"] is None


# ── The allowlist and the schemas ──────────────────────────────────────────


def test_the_read_allowlist_is_exactly_the_audited_set():
    from robothor.engine.tools.constants import CROSS_TENANT_READS

    assert (
        frozenset(
            {
                # Observability: agent_runs, agent_schedules, agent_reviews and
                # benchmark_results, each filtered on ctx.tenant_id.
                "list_agent_runs",
                "get_agent_run",
                "list_agent_schedules",
                "get_agent_stats",
                "list_agent_reviews",
                "get_agent_review",
                "get_agent_performance_summary",
                # CRM reads through the DAL with tenant_id=ctx.tenant_id.
                "list_tasks",
                "get_task",
                "list_tasks_summary",
                "list_notes",
                "get_note",
                "list_people",
                "get_person",
                "list_companies",
                "get_company",
                "list_conversations",
                "get_conversation",
                "list_messages",
                "search_records",
                "get_contact_360",
                "list_contact_messages",
                # devops_metrics_snapshots by ctx.tenant_id.
                "devops_query_metrics",
                # Datadog logs, bounded by scope_for(ctx.tenant_id).
                "datadog_logs_search",
                "datadog_logs_aggregate",
                # Datadog spans, one trace, monitors, metrics: same scope_for.
                "datadog_spans_search",
                "datadog_trace_get",
                "datadog_monitors_list",
                "datadog_metrics_query",
            }
        )
        == CROSS_TENANT_READS
    )


def test_every_allowlisted_read_has_a_handler():
    from robothor.engine.tools.constants import CROSS_TENANT_READS
    from robothor.engine.tools.dispatch import builtin_handlers

    assert set(builtin_handlers()) >= CROSS_TENANT_READS


def test_allowlisted_tools_advertise_an_optional_tenant_argument():
    from robothor.engine.tools.constants import CROSS_TENANT_READS
    from robothor.engine.tools.registry import builtin_schemas

    schemas = builtin_schemas()
    for name in CROSS_TENANT_READS:
        params = schemas[name]["function"]["parameters"]
        assert "tenant" in params["properties"], name
        assert params["properties"]["tenant"]["type"] == "string"
        assert "tenant" not in params.get("required", []), name
    assert "tenant" not in schemas["write_file"]["function"]["parameters"]["properties"]


def test_injecting_the_tenant_argument_does_not_mutate_the_shared_definitions():
    from robothor.api.mcp import get_tool_definitions
    from robothor.engine.tools.registry import builtin_schemas

    builtin_schemas()
    for defn in get_tool_definitions():
        if defn["name"] == "list_tasks":
            assert "tenant" not in defn["inputSchema"].get("properties", {})


# ── The tenant argument's shape, and crossings answered without the handler ─


@pytest.mark.parametrize(
    "value",
    ["x" * 100_000, "acme-" + "a" * 124, 123, ["acme-child-a"], {"id": "acme-child-a"}, True],
    ids=["huge", "129-chars", "int", "list", "dict", "bool"],
)
def test_a_malformed_tenant_gets_the_unreachable_refusal(env, value):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    result = _call("list_tasks", {"tenant": value})

    assert result.get("refused") is True, result
    assert result["error"] == UNREACHABLE
    assert env.seen.calls == []
    env.audit.assert_called_once()
    audited = env.audit.call_args.kwargs["target_tenant"]
    assert isinstance(audited, str) and len(audited) <= 128


def test_a_128_character_reachable_tenant_is_still_a_tenant(env):
    long_child = "c" * 128
    result = _call("list_tasks", {"tenant": long_child}, accessible_tenant_ids=(PARENT, long_child))
    assert "error" not in result, result


def test_a_repeat_guard_answer_from_a_child_is_audited(env):
    from robothor.engine.repeat_guard import GuardDecision

    guard = MagicMock()
    guard.before.return_value = GuardDecision(
        action="answered", tool_name="list_tasks", note="same call", result={"tasks": []}
    )
    with patch("robothor.engine.repeat_guard.guard_for_run", return_value=guard):
        result = _call("list_tasks", {"tenant": CHILD_A})

    assert result == {"tasks": []}
    assert env.seen.calls == []
    env.audit.assert_called_once()
    kw = env.audit.call_args.kwargs
    assert (kw["home_tenant"], kw["target_tenant"]) == (PARENT, CHILD_A)
    assert "repeat" in kw["outcome"]


@dataclass
class _EffectContext:
    tenant_id: str
    principal_id: str


def test_an_effect_replay_from_a_child_is_audited():
    from robothor.engine.runtime import effect_dispatch

    ctx = SimpleNamespace(
        tenant_id=PARENT, run_id="run-1", agent_id="main", user_id="alice", is_benchmark=False
    )
    record = {"id": "e-1", "state": "finished", "resolution": {"result": {"id": "t-1"}}}
    context = _EffectContext(tenant_id=PARENT, principal_id="alice")
    dispatch = AsyncMock()
    with (
        patch.object(effect_dispatch, "active_context", MagicMock(get=lambda: context)),
        patch.object(effect_dispatch, "_reserve", AsyncMock(return_value=record)),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        result = asyncio.run(
            effect_dispatch.invoke(
                "send_notification", {"tenant": CHILD_A, "subject": "s"}, ctx, dispatch
            )
        )

    dispatch.assert_not_awaited()
    assert result["recovered"] is True
    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert (kw["home_tenant"], kw["target_tenant"]) == (PARENT, CHILD_A)
    assert "replay" in kw["outcome"]


def test_an_own_tenant_replay_is_not_a_crossing():
    from robothor.engine.runtime import effect_dispatch

    ctx = SimpleNamespace(
        tenant_id=PARENT, run_id="run-1", agent_id="main", user_id="alice", is_benchmark=False
    )
    record = {"id": "e-1", "state": "finished", "resolution": {"result": {"id": "t-1"}}}
    context = _EffectContext(tenant_id=PARENT, principal_id="alice")
    with (
        patch.object(effect_dispatch, "active_context", MagicMock(get=lambda: context)),
        patch.object(effect_dispatch, "_reserve", AsyncMock(return_value=record)),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        asyncio.run(effect_dispatch.invoke("send_notification", {"subject": "s"}, ctx, AsyncMock()))

    audit.assert_not_called()
