"""Cross-tenant reads against real Postgres, with RLS enforcing as robothor_app.

The unit tests prove dispatch swaps the scope; these prove the scope is what
the DATABASE enforces. Two children of the platform tenant each hold a task.
From a root process (the platform tenant), a cross-tenant tool call naming
child A, whose handler runs a query with NO tenant predicate at all, gets
child A's rows and nothing else: row-level security, not the handler's WHERE
clause, is what keeps child B out. The rollup is run on the same data, so its
SQL is exercised against the real schema too.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import TriggerType
from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration


def _seed(db_conn: Any, ids: dict[str, str]) -> None:
    with db_conn.cursor() as cur:
        for key in ("a", "b"):
            cur.execute(
                "INSERT INTO crm_tasks (tenant_id, title, due_at) "
                "VALUES (%s, %s, NOW() - INTERVAL '3 days')",
                (ids[key], f"Task in {key}"),
            )
            cur.execute(
                "INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, status) "
                "VALUES (%s, 'main', 'manual', 'completed')",
                (ids[key],),
            )


def _unfiltered_task_tenants() -> list[str]:
    from robothor.db.connection import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        # Deliberately no tenant predicate: only RLS limits this.
        cur.execute("SELECT DISTINCT tenant_id FROM crm_tasks")
        return sorted(r[0] for r in cur.fetchall())


async def _handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    tenants = await asyncio.to_thread(_unfiltered_task_tenants)
    return {"tasks": [], "tenants_seen": tenants}


def test_a_cross_tenant_read_sees_only_the_target_tenants_rows(db_conn, app_role, rls_tenants):  # noqa: F811
    from robothor.engine.tools.dispatch import _execute_tool

    ids = rls_tenants
    _seed(db_conn, ids)
    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    session = SimpleNamespace(
        run=SimpleNamespace(
            parent_run_id=None,
            nesting_depth=0,
            trigger_type=TriggerType.WEBCHAT,
            is_benchmark=False,
        )
    )
    with (
        patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch(
            "robothor.engine.tools.dispatch._get_handlers", return_value={"list_tasks": _handler}
        ),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant"),
        patch("robothor.engine.session_registry.lookup", return_value=session),
    ):
        result = asyncio.run(
            _execute_tool(
                "list_tasks",
                {"tenant": ids["a"]},
                agent_id="main",
                run_id="run-1",
                tenant_id=ids["root"],
                user_id="alice",
                user_role="owner",
                accessible_tenant_ids=(ids["root"], ids["a"], ids["b"]),
            )
        )

    assert result["tenants_seen"] == [ids["a"]]
    # And the root process, outside the call, does not see the children at all.
    assert ids["a"] not in _unfiltered_task_tenants()
    assert ids["b"] not in _unfiltered_task_tenants()


def test_the_rollup_reads_each_child_in_its_own_scope(db_conn, app_role, rls_tenants):  # noqa: F811
    from robothor.tenancy.rollup import tenant_rollup

    ids = rls_tenants
    _seed(db_conn, ids)

    result = tenant_rollup([ids["a"], ids["b"]])

    entries = {t["tenant_id"]: t for t in result["tenants"]}
    for key in ("a", "b"):
        entry = entries[ids[key]]
        assert "error" not in entry, entry
        assert entry["tasks"]["open"] == 1
        assert entry["tasks"]["overdue"] == 1
        assert [t["title"] for t in entry["tasks"]["oldest_overdue"]] == [f"Task in {key}"]
        assert entry["tasks"]["oldest_overdue"][0]["overdueDays"] == 3
        assert entry["runs_7d"] == 1
        assert entry["last_activity_at"] is not None
