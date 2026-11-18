"""The organisation rollup: one health entry per tenant, each read in its scope.

A parent tenant's owner asked how the companies were doing and the agent could
see only parent rows. ``tenant_rollup`` answers that question for a list of
tenants. Every read for a tenant happens inside ``tenant_scope(tenant)`` so
row-level security limits it to that tenant; the fakes below record the scope
they were called in, which is what these tests assert.

The analytics and DAL reads are faked at the modules the rollup imports them
from. No database is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.db.connection import current_tenant_scope

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

_TENANTS = [
    {"id": "acme-parent", "displayName": "Acme Parent", "parentTenantId": None},
    {"id": "acme-child-a", "displayName": "Acme Child A", "parentTenantId": "acme-parent"},
    {"id": "acme-child-b", "displayName": "Acme Child B", "parentTenantId": "acme-parent"},
]


def _health(total: int, completed: int, failed: int, cost: float) -> dict[str, Any]:
    return {
        "agents": [],
        "fleet_totals": {
            "total_runs": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(completed / total, 4) if total else None,
            "total_cost_usd": cost,
            "benchmark_runs": None,
            "benchmark_cost_usd": None,
        },
    }


class _World:
    """Per-tenant fake data, and a record of the scope every read ran in."""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, str, str | None]] = []
        self.health = {
            ("acme-child-a", 1): _health(10, 8, 2, 0.5),
            ("acme-child-a", 7): _health(70, 60, 10, 3.25),
            ("acme-child-b", 1): _health(0, 0, 0, 0.0),
            ("acme-child-b", 7): _health(0, 0, 0, 0.0),
            ("acme-parent", 1): _health(4, 4, 0, 0.1),
            ("acme-parent", 7): _health(20, 20, 0, 0.9),
        }
        self.fail_for: set[str] = set()
        self.include_benchmark_seen: list[bool] = []

    def _seen(self, what: str, tenant_id: str) -> None:
        self.scopes.append((what, tenant_id, current_tenant_scope()))
        if tenant_id in self.fail_for:
            raise RuntimeError("database unavailable")

    def fleet_health(
        self, days: int = 1, tenant_id: str = "", include_benchmark: bool = True
    ) -> dict[str, Any]:
        self._seen("fleet_health", tenant_id)
        self.include_benchmark_seen.append(include_benchmark)
        return self.health[(tenant_id, days)]

    def failure_patterns(self, hours: int = 24, tenant_id: str = "") -> dict[str, Any]:
        self._seen("failure_patterns", tenant_id)
        if tenant_id == "acme-child-a":
            return {
                "patterns": [
                    {"agent_id": "triage", "error_type": "timeout", "count": 2},
                ],
                "total_clusters": 1,
                "period_hours": hours,
            }
        return {"patterns": [], "total_clusters": 0, "period_hours": hours}

    def schedules(self, enabled_only: bool = False, tenant_id: str = "") -> list[dict[str, Any]]:
        self._seen("schedules", tenant_id)
        if tenant_id != "acme-child-a":
            return []
        return [
            {
                "agent_id": "triage",
                "enabled": True,
                "next_run_at": NOW - timedelta(hours=3),
                "consecutive_errors": 3,
                "last_status": "failed",
            },
            {
                "agent_id": "digest",
                "enabled": True,
                "next_run_at": NOW + timedelta(hours=1),
                "consecutive_errors": 0,
                "last_status": "completed",
            },
            {
                # Disabled: never overdue, whatever its next_run_at says.
                "agent_id": "retired",
                "enabled": False,
                "next_run_at": NOW - timedelta(days=30),
                "consecutive_errors": 0,
                "last_status": "completed",
            },
        ]

    def list_tasks(self, **kw: Any) -> list[dict[str, Any]]:
        self._seen("list_tasks", kw.get("tenant_id", ""))
        if kw.get("tenant_id") != "acme-child-a":
            return []
        assert kw.get("overdue") is True
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "Renew the lease",
                "status": "TODO",
                "dueAt": (NOW - timedelta(days=5)).isoformat(),
                "createdAt": (NOW - timedelta(days=12)).isoformat(),
                "assignedToAgent": "main",
                "body": "long body that the overview must not carry",
            }
        ]

    def task_counts(self, tenant_id: str) -> dict[str, int]:
        self._seen("task_counts", tenant_id)
        return {"acme-child-a": {"open": 7, "overdue": 1}}.get(tenant_id, {"open": 0, "overdue": 0})

    def last_activity(self, tenant_id: str) -> datetime | None:
        self._seen("last_activity", tenant_id)
        return {
            "acme-child-a": NOW - timedelta(minutes=5),
            "acme-child-b": NOW - timedelta(days=40),
            "acme-parent": NOW - timedelta(hours=1),
        }.get(tenant_id)


@pytest.fixture
def world():
    w = _World()
    with (
        patch("robothor.engine.analytics.get_fleet_health", side_effect=w.fleet_health),
        patch("robothor.engine.analytics.get_failure_patterns", side_effect=w.failure_patterns),
        patch("robothor.engine.tracking.list_schedules", side_effect=w.schedules),
        patch("robothor.crm.dal.list_tasks", side_effect=w.list_tasks),
        patch("robothor.crm.dal.list_tenants", return_value=_TENANTS),
        patch("robothor.tenancy.rollup._task_counts", side_effect=w.task_counts),
        patch("robothor.tenancy.rollup._last_activity_at", side_effect=w.last_activity),
    ):
        yield w


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["tenant_id"]: t for t in result["tenants"]}


def test_every_requested_tenant_is_reported_with_its_name_and_parent(world):
    from robothor.tenancy.rollup import tenant_rollup

    result = tenant_rollup(["acme-parent", "acme-child-a", "acme-child-b"], now=NOW)

    assert result["as_of"] == NOW.isoformat()
    entries = _by_id(result)
    assert set(entries) == {"acme-parent", "acme-child-a", "acme-child-b"}
    assert entries["acme-child-a"]["display_name"] == "Acme Child A"
    assert entries["acme-child-a"]["parent_id"] == "acme-parent"
    assert entries["acme-parent"]["parent_id"] is None


def test_every_read_for_a_tenant_runs_inside_that_tenants_scope(world):
    from robothor.tenancy.rollup import tenant_rollup

    tenant_rollup(["acme-parent", "acme-child-a"], now=NOW)

    assert world.scopes, "the rollup read nothing"
    for what, tenant, scope in world.scopes:
        assert scope == tenant, f"{what} for {tenant} ran in scope {scope!r}"
    # And the scope is released afterwards.
    assert current_tenant_scope() is None


def test_runs_failures_success_rate_and_cost_for_both_windows(world):
    from robothor.tenancy.rollup import tenant_rollup

    entry = _by_id(tenant_rollup(["acme-child-a"], now=NOW))["acme-child-a"]

    assert entry["runs_24h"] == 10
    assert entry["failures_24h"] == 2
    assert entry["runs_7d"] == 70
    assert entry["failures_7d"] == 10
    assert entry["success_rate_7d"] == pytest.approx(60 / 70, abs=1e-3)
    assert entry["cost_usd_24h"] == pytest.approx(0.5)
    assert entry["cost_usd_7d"] == pytest.approx(3.25)
    assert entry["top_failures"][0]["agent_id"] == "triage"


def test_the_all_tenant_benchmark_breakout_is_never_run_per_company(world):
    from robothor.tenancy.rollup import tenant_rollup

    tenant_rollup(["acme-parent", "acme-child-a", "acme-child-b"], now=NOW)

    assert world.include_benchmark_seen
    assert not any(world.include_benchmark_seen)
    entry = _by_id(tenant_rollup(["acme-child-a"], now=NOW))["acme-child-a"]
    assert "benchmark_runs" not in entry
    assert "benchmark_cost_usd" not in entry


def test_agents_with_consecutive_errors_and_overdue_schedules(world):
    from robothor.tenancy.rollup import tenant_rollup

    entry = _by_id(tenant_rollup(["acme-child-a"], now=NOW))["acme-child-a"]

    assert [a["agent_id"] for a in entry["agents_with_consecutive_errors"]] == ["triage"]
    assert entry["agents_with_consecutive_errors"][0]["consecutive_errors"] == 3
    overdue = entry["overdue_schedules"]
    assert [s["agent_id"] for s in overdue] == ["triage"]
    assert overdue[0]["overdue_minutes"] == 180


def test_open_and_overdue_tasks_with_age_in_days(world):
    from robothor.tenancy.rollup import tenant_rollup

    entry = _by_id(tenant_rollup(["acme-child-a"], now=NOW))["acme-child-a"]

    tasks = entry["tasks"]
    assert tasks["open"] == 7
    assert tasks["overdue"] == 1
    oldest = tasks["oldest_overdue"][0]
    assert oldest["title"] == "Renew the lease"
    assert oldest["overdueDays"] == 5
    assert oldest["ageDays"] == 12
    assert "body" not in oldest


def test_a_tenant_with_no_activity_is_reported_as_such_not_as_healthy(world):
    from robothor.tenancy.rollup import tenant_rollup

    entry = _by_id(tenant_rollup(["acme-child-b"], now=NOW))["acme-child-b"]

    assert entry["status"] == "no_activity"
    assert entry["last_activity_at"] == (NOW - timedelta(days=40)).isoformat()
    assert entry["success_rate_7d"] is None


def test_status_names_attention_and_ok(world):
    from robothor.tenancy.rollup import tenant_rollup

    entries = _by_id(tenant_rollup(["acme-parent", "acme-child-a"], now=NOW))

    assert entries["acme-child-a"]["status"] == "attention"
    assert entries["acme-parent"]["status"] == "ok"


def test_one_tenants_failure_does_not_fail_the_rollup(world):
    from robothor.tenancy.rollup import tenant_rollup

    world.fail_for = {"acme-child-a"}
    entries = _by_id(tenant_rollup(["acme-parent", "acme-child-a", "acme-child-b"], now=NOW))

    assert entries["acme-child-a"]["status"] == "error"
    assert entries["acme-child-a"]["error"]
    # The name still comes from crm_tenants, so the reader knows which company.
    assert entries["acme-child-a"]["display_name"] == "Acme Child A"
    assert entries["acme-parent"]["status"] == "ok"
    assert entries["acme-child-b"]["status"] == "no_activity"


def test_duplicates_are_reported_once_in_the_order_given(world):
    from robothor.tenancy.rollup import tenant_rollup

    result = tenant_rollup(["acme-parent", "acme-child-a", "acme-parent"], now=NOW)

    assert [t["tenant_id"] for t in result["tenants"]] == ["acme-parent", "acme-child-a"]


def test_the_rollup_does_not_import_engine_tool_modules():
    """The dashboard bridge imports this module; a tool module would drag the engine in."""
    import ast
    from pathlib import Path

    import robothor.tenancy.rollup as rollup

    tree = ast.parse(Path(rollup.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not [m for m in imported if m.startswith("robothor.engine.tools")]


class TestFleetHealthWithoutTheBenchmarkBreakout:
    """``include_benchmark=False`` skips the deliberately all-tenant query."""

    @patch("robothor.engine.analytics.read_every_tenant_in_transaction")
    @patch("robothor.engine.analytics.get_connection")
    def test_no_all_tenant_read(self, mock_get_conn, mock_widen):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        from robothor.engine.analytics import get_fleet_health

        result = get_fleet_health(days=1, tenant_id="acme-child-a", include_benchmark=False)

        mock_widen.assert_not_called()
        assert cursor.execute.call_count == 1
        assert result["fleet_totals"]["benchmark_runs"] is None
