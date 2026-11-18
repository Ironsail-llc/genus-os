"""``org_watch`` — what changed across the organisation since the last look.

The parent's main agent calls it on its heartbeat and tells the owner only when
the briefing is not empty. The diff and the wording are covered in
``robothor/tenancy/tests/test_watch.py``; these tests cover the tool: which
tenants it looks at, that ``preview`` never stores, that a run without reach
says so, and how it is classified.
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
        "trigger_type": TriggerType.CRON,
        "is_benchmark": False,
    }
    base.update(run)
    return SimpleNamespace(run=SimpleNamespace(**base))


def _ctx(**kw: Any) -> ToolContext:
    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": "",
        "user_role": "",
        "accessible_tenant_ids": (PARENT, CHILD_A, CHILD_B),
    }
    base.update(kw)
    return ToolContext(**base)


class _Rollup:
    """Every tenant healthy, except the ones named in ``failing``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.failing: set[str] = set()

    def __call__(self, tenant_ids: Any, **_: Any) -> dict[str, Any]:
        ids = list(tenant_ids)
        self.calls.append(ids)
        return {"as_of": "2026-11-18T12:00:00+00:00", "tenants": [self._entry(t) for t in ids]}

    def _entry(self, tenant_id: str) -> dict[str, Any]:
        failing = tenant_id in self.failing
        return {
            "tenant_id": tenant_id,
            "display_name": tenant_id.replace("-", " ").title(),
            "parent_id": None,
            "runs_24h": 5,
            "failures_24h": 2 if failing else 0,
            "runs_7d": 30,
            "failures_7d": 2 if failing else 0,
            "top_failures": (
                [{"agent_id": "triage", "error_type": "timeout", "count": 2}] if failing else []
            ),
            "agents_with_consecutive_errors": [],
            "overdue_schedules": [],
            "tasks": {"open": 0, "overdue": 0, "oldest_overdue": []},
            "last_activity_at": "2026-11-18T11:00:00+00:00",
            "status": "attention" if failing else "ok",
        }


class _Blocks:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.writes: list[tuple[str, str]] = []

    def read_blocks(self, names: list[str], tenant_id: str = "") -> dict[str, Any]:
        return {
            n: {"block_name": n, "content": self.rows[(tenant_id, n)]}
            for n in names
            if (tenant_id, n) in self.rows
        }

    def write_block(self, name: str, content: str, tenant_id: str = "") -> dict[str, Any]:
        self.rows[(tenant_id, name)] = content
        self.writes.append((tenant_id, name))
        return {"success": True, "block_name": name}


@pytest.fixture
def world():
    rollup, blocks = _Rollup(), _Blocks()
    with (
        patch("robothor.tenancy.rollup.tenant_rollup", side_effect=rollup),
        patch("robothor.memory.blocks.read_blocks", side_effect=blocks.read_blocks),
        patch("robothor.memory.blocks.write_block", side_effect=blocks.write_block),
    ):
        yield SimpleNamespace(rollup=rollup, blocks=blocks)


def _run(args: dict[str, Any], ctx: ToolContext | None = None, session: Any = "default") -> Any:
    from robothor.engine.tools.handlers.org import HANDLERS

    live = _session() if session == "default" else session
    with patch("robothor.engine.session_registry.lookup", return_value=live):
        return asyncio.run(HANDLERS["org_watch"](args, ctx or _ctx()))


def test_watches_every_reachable_tenant(world):
    result = _run({})

    assert world.rollup.calls == [[PARENT, CHILD_A, CHILD_B]]
    assert result["tenants_seen"] == 3
    assert result["as_of"] == "2026-11-18T12:00:00+00:00"
    assert result["limited"] is False
    assert "note" not in result


def test_reports_a_change_once_and_stores_the_snapshot_in_the_run_tenant(world):
    _run({})
    world.rollup.failing.add(CHILD_A)

    first = _run({})
    second = _run({})

    assert first["changes"] and "Acme Child A" in first["briefing"]
    assert second["changes"] == [] and second["briefing"] == ""
    assert {tenant for tenant, _ in world.blocks.writes} == {PARENT}


def test_preview_does_not_store_so_the_change_is_still_reported_later(world):
    _run({})
    writes = len(world.blocks.writes)
    world.rollup.failing.add(CHILD_A)

    preview = _run({"preview": True})
    real = _run({})

    assert preview["changes"] and preview["briefing"]
    assert real["changes"] == preview["changes"]
    assert len(world.blocks.writes) == writes + 1


def test_daily_reports_every_company_even_when_nothing_changed(world):
    _run({})

    result = _run({"daily": True})

    assert result["changes"] == []
    for name in ("Acme Parent", "Acme Child A", "Acme Child B"):
        assert name in result["briefing"]


def test_a_run_without_child_reach_is_told_it_is_limited(world):
    result = _run({}, _ctx(accessible_tenant_ids=(PARENT,)))

    assert world.rollup.calls == [[PARENT]]
    assert result["limited"] is True
    assert "child" in result["note"].lower()
    assert "not granted" in result["note"].lower()


def test_a_sub_agent_is_limited_to_its_own_tenant(world):
    result = _run({}, session=_session(parent_run_id="parent-run", nesting_depth=1))

    assert world.rollup.calls == [[PARENT]]
    assert result["limited"] is True


def test_reaching_children_is_audited_as_a_cross_tenant_read(world):
    with patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit:
        _run({"preview": True})

    assert {c.kwargs["target_tenant"] for c in audit.call_args_list} == {CHILD_A, CHILD_B}
    assert {c.kwargs["tool"] for c in audit.call_args_list} == {"org_watch"}


def test_registered_not_read_only_and_kept_from_benchmarks():
    """It writes its snapshot, so plan mode and graded runs must not call it."""
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.handlers.benchmark import (
        _BENCHMARK_EXCLUDED_TOOLS,
        benchmark_readonly_tools,
    )
    from robothor.engine.tools.schemas import get_engine_schemas

    assert "org_watch" in builtin_handlers()
    assert "org_watch" in get_engine_schemas()
    assert "org_watch" not in READONLY_TOOLS
    assert "org_watch" in _BENCHMARK_EXCLUDED_TOOLS
    assert "org_watch" not in benchmark_readonly_tools()


def test_schema_declares_daily_and_preview():
    from robothor.engine.tools.schemas import get_engine_schemas

    params = get_engine_schemas()["org_watch"]["function"]["parameters"]["properties"]
    assert params["daily"]["type"] == "boolean"
    assert params["preview"]["type"] == "boolean"
