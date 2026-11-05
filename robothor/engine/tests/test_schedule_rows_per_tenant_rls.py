"""Two tenants holding one job id, as the engine's non-superuser role sees it.

Every tenant engine declares the same stock agents, so the same job id reaches
``agent_schedules`` from several tenants. These go through the real tracking
functions, the real ``_apply_tenant_scope`` and ``robothor_app``, because the
defect only exists where row-level security is enforcing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from robothor.db.connection import tenant_scope, unbound_scope
from robothor.engine.tracking import (
    delete_stale_schedules,
    get_schedule,
    list_schedules,
    update_schedule_state,
    upsert_schedule,
)
from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration

JOB = "alice-agent"


@pytest.fixture
def tenants(db_conn, app_role, rls_tenants) -> Iterator[dict[str, str]]:  # noqa: F811
    """``root`` already holds the job, as the first tenant to write it does."""
    with tenant_scope(rls_tenants["root"]):
        upsert_schedule(JOB, tenant_id=rls_tenants["root"], cron_expr="0 * * * *")
    yield rls_tenants


def _row(tenant: str) -> dict[str, Any] | None:
    with tenant_scope(tenant):
        return get_schedule(JOB, tenant_id=tenant)


def test_a_second_tenant_can_write_a_job_id_another_tenant_holds(tenants) -> None:
    with tenant_scope(tenants["a"]):
        assert upsert_schedule(JOB, tenant_id=tenants["a"], cron_expr="5 * * * *") is True
        assert [r["cron_expr"] for r in list_schedules(tenant_id=tenants["a"])] == ["5 * * * *"]

    with tenant_scope(tenants["root"]):
        assert [r["cron_expr"] for r in list_schedules(tenant_id=tenants["root"])] == ["0 * * * *"]


def test_a_tenant_rewrites_its_own_row_in_place(tenants) -> None:
    for cron in ("5 * * * *", "6 * * * *"):
        with tenant_scope(tenants["a"]):
            upsert_schedule(JOB, tenant_id=tenants["a"], cron_expr=cron)

    with tenant_scope(tenants["a"]):
        assert [r["cron_expr"] for r in list_schedules(tenant_id=tenants["a"])] == ["6 * * * *"]


def test_each_tenant_reads_and_counts_its_own_row(tenants) -> None:
    with tenant_scope(tenants["a"]):
        upsert_schedule(JOB, tenant_id=tenants["a"], cron_expr="5 * * * *")
        assert update_schedule_state(JOB, tenant_id=tenants["a"], consecutive_errors=4) is True

    row = _row(tenants["a"])
    assert row is not None
    assert row["consecutive_errors"] == 4
    root_row = _row(tenants["root"])
    assert root_row is not None
    assert root_row["consecutive_errors"] == 0


def test_the_statements_scope_themselves_with_no_policy_in_force(tenants) -> None:
    """An unbound connection (a root shell, the migrator) passes the policy, so
    only the statement's own predicate keeps two tenants' rows apart."""
    with tenant_scope(tenants["a"]):
        upsert_schedule(JOB, tenant_id=tenants["a"], cron_expr="5 * * * *")

    with unbound_scope():
        update_schedule_state(JOB, tenant_id=tenants["a"], consecutive_errors=4)
        mine = get_schedule(JOB, tenant_id=tenants["a"])
        theirs = get_schedule(JOB, tenant_id=tenants["root"])

    assert mine is not None
    assert mine["tenant_id"] == tenants["a"]
    assert mine["consecutive_errors"] == 4
    assert theirs is not None
    assert theirs["tenant_id"] == tenants["root"]
    assert theirs["consecutive_errors"] == 0


def test_pruning_one_tenant_leaves_the_others_row(tenants) -> None:
    with tenant_scope(tenants["a"]):
        upsert_schedule(JOB, tenant_id=tenants["a"], cron_expr="5 * * * *")

    with tenant_scope(tenants["root"]):
        assert delete_stale_schedules({"bob-agent"}, tenant_id=tenants["root"]) == [JOB]

    assert _row(tenants["a"]) is not None
