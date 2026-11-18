"""``org_overview`` — the health of every tenant this run may reach, in one call.

The parent's owner asked the parent's agent how the companies were doing, and
the agent could see only the parent's rows. This tool returns the rollup
(``robothor.tenancy.rollup``) for the run's own tenant plus every tenant in
``ctx.accessible_tenant_ids`` — and never more than the own tenant for a
sub-agent or a benchmark run, whatever the context carries.

The rollup itself is faked; ``robothor/tenancy/tests/test_rollup.py`` covers it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.models import TriggerType
from robothor.engine.tools.dispatch import ToolContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"


def _session(**run: Any) -> SimpleNamespace:
    base = {
        "parent_run_id": None,
        "nesting_depth": 0,
        "trigger_type": TriggerType.WEBCHAT,
        "is_benchmark": False,
    }
    base.update(run)
    return SimpleNamespace(run=SimpleNamespace(**base))


def _ctx(**kw: Any) -> ToolContext:
    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": "alice",
        "user_role": "owner",
        "accessible_tenant_ids": (PARENT, CHILD_A, CHILD_B),
    }
    base.update(kw)
    return ToolContext(**base)


class _Rollup:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, tenant_ids: Any, **_: Any) -> dict[str, Any]:
        ids = list(tenant_ids)
        self.calls.append(ids)
        return {
            "as_of": "2026-11-18T12:00:00+00:00",
            "tenants": [{"tenant_id": t, "status": "ok"} for t in ids],
        }


@pytest.fixture
def rollup():
    fake = _Rollup()
    with patch("robothor.tenancy.rollup.tenant_rollup", side_effect=fake):
        yield fake


def _run(args: dict[str, Any], ctx: ToolContext, session: Any) -> dict[str, Any]:
    from robothor.engine.tools.handlers.org import HANDLERS

    with patch("robothor.engine.session_registry.lookup", return_value=session):
        return asyncio.run(HANDLERS["org_overview"](args, ctx))


def test_reports_own_tenant_and_every_reachable_child(rollup):
    result = _run({}, _ctx(), _session())

    assert rollup.calls == [[PARENT, CHILD_A, CHILD_B]]
    assert [t["tenant_id"] for t in result["tenants"]] == [PARENT, CHILD_A, CHILD_B]
    assert "note" not in result


def test_own_tenant_always_first_even_if_missing_from_reach(rollup):
    _run({}, _ctx(accessible_tenant_ids=(CHILD_A,)), _session())

    assert rollup.calls == [[PARENT, CHILD_A]]


def test_no_reach_returns_own_tenant_with_a_plain_note(rollup):
    result = _run({}, _ctx(user_role="member", accessible_tenant_ids=(PARENT,)), _session())

    assert rollup.calls == [[PARENT]]
    assert isinstance(result["note"], str) and result["note"]


def test_a_sub_agent_never_reaches_beyond_its_own_tenant(rollup):
    result = _run({}, _ctx(), _session(parent_run_id="parent-run", nesting_depth=1))

    assert rollup.calls == [[PARENT]]
    assert result["note"]


def test_a_sub_agent_with_no_recorded_parent_is_still_a_sub_agent(rollup):
    # tracking_disabled parents give their children parent_run_id=None.
    _run({}, _ctx(), _session(trigger_type=TriggerType.SUB_AGENT, nesting_depth=1))

    assert rollup.calls == [[PARENT]]


def test_a_benchmark_run_never_reaches_beyond_its_own_tenant(rollup):
    _run({}, _ctx(is_benchmark=True), _session(is_benchmark=True))

    assert rollup.calls == [[PARENT]]


def test_a_call_outside_any_live_run_reaches_only_its_own_tenant(rollup):
    _run({}, _ctx(), None)

    assert rollup.calls == [[PARENT]]


def test_reaching_children_is_audited_as_a_cross_tenant_read(rollup):
    with patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit:
        _run({}, _ctx(), _session())

    targets = {c.kwargs["target_tenant"] for c in audit.call_args_list}
    assert targets == {CHILD_A, CHILD_B}
    for c in audit.call_args_list:
        assert c.kwargs["home_tenant"] == PARENT
        assert c.kwargs["tool"] == "org_overview"
        assert c.kwargs["user_id"] == "alice"
        assert c.kwargs["outcome"] == "ok"


def test_registered_read_only_and_withheld_from_benchmarks():
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.handlers.benchmark import _BENCHMARK_WITHHELD_READS
    from robothor.engine.tools.schemas import get_engine_schemas

    assert "org_overview" in builtin_handlers()
    assert "org_overview" in READONLY_TOOLS
    assert "org_overview" in _BENCHMARK_WITHHELD_READS
    assert "org_overview" in get_engine_schemas()
