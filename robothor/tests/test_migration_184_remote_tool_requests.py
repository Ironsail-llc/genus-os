"""184: the remote-tool request ledger, and the parent_agent role denied by default.

``remote_tool_requests`` is what makes a parent's write run at most once in this
tenant: a request id is claimed ``running`` before the tool runs and becomes
``done`` with its answer after. Tenant-keyed with forced row-level security,
like every sibling tenant table.

``parent_agent`` is the role a person-less read from the parent runs as. It is
denied everything by default; a tenant opens it one tool at a time with a
tenant-scoped allow row.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATION = "184_remote_tool_requests"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"


def test_the_migration_is_in_the_manifest() -> None:
    assert SQL_FILE.exists()
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


def test_the_table_is_classified_for_workload_moves() -> None:
    from robothor.tenancy.move_workload import MOVED_TABLES, REFUSED_IF_PRESENT, UNTOUCHED_TABLES

    known = set(MOVED_TABLES) | set(UNTOUCHED_TABLES) | set(REFUSED_IF_PRESENT)
    assert "remote_tool_requests" in known


def _connect(dsn: str, monkeypatch) -> None:
    import psycopg2

    @contextlib.contextmanager
    def connect(**_kw: Any) -> Any:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr("robothor.db.connection.get_connection", connect)


@pytest.mark.integration
def test_it_applies_twice_with_forced_row_level_security(scratch_db) -> None:
    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur:
        cur.execute(SQL_FILE.read_text())
        cur.execute(SQL_FILE.read_text())
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'remote_tool_requests'::regclass"
        )
        assert cur.fetchone() == (True, True)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'remote_tool_requests'"
        )
        assert {r[0] for r in cur.fetchall()} >= {
            "tenant_id", "request_id", "state", "result", "created_at", "finished_at",
        }  # fmt: skip


@pytest.mark.integration
def test_only_the_two_states_are_accepted(scratch_db) -> None:
    import psycopg2

    db, _ = scratch_db(through=MIGRATION)
    with db.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO remote_tool_requests (tenant_id, request_id, state) "
            "VALUES ('test-tenant', 'r', 'pending')"
        )


@pytest.mark.integration
def test_the_ledger_claims_finishes_and_releases(scratch_db, monkeypatch) -> None:
    from robothor.engine import remote_tools

    _, dsn = scratch_db(through=MIGRATION)
    _connect(dsn, monkeypatch)

    assert remote_tools.claim_request("test-tenant", "req-1") is None  # claimed
    assert remote_tools.claim_request("test-tenant", "req-1") == {
        "state": "running",
        "result": None,
    }
    assert remote_tools.claim_request("test-tenant-other", "req-1") is None  # another tenant's

    remote_tools.finish_request("test-tenant", "req-1", {"status": 200, "body": {"x": 1}})
    assert remote_tools.claim_request("test-tenant", "req-1") == {
        "state": "done",
        "result": {"status": 200, "body": {"x": 1}},
    }

    remote_tools.release_request("test-tenant", "req-1")  # a finished row is kept
    assert remote_tools.claim_request("test-tenant", "req-1")["state"] == "done"
    remote_tools.release_request("test-tenant-other", "req-1")  # a running one is released
    assert remote_tools.claim_request("test-tenant-other", "req-1") is None


@pytest.mark.integration
def test_parent_agent_is_denied_everything_until_a_tenant_grants_a_tool(
    scratch_db, monkeypatch
) -> None:
    from robothor.engine import permissions
    from robothor.tenancy.remote_contract import ROLE_PARENT_AGENT

    db, dsn = scratch_db(through=MIGRATION)
    _connect(dsn, monkeypatch)
    permissions.invalidate_permission_cache()

    def decision(tool: str) -> str | None:
        permissions.invalidate_permission_cache()
        return permissions.check_tool_permission(ROLE_PARENT_AGENT, "test-tenant", tool)

    for tool in ("example_read", "search_people", "exec", "list_tasks"):
        assert decision(tool) is not None, tool

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) "
            "VALUES ('test-tenant', %s, 'example_read', 'allow')",
            (ROLE_PARENT_AGENT,),
        )
    assert decision("example_read") is None
    assert decision("example_write") is not None
    permissions.invalidate_permission_cache()
    assert (
        permissions.check_tool_permission(ROLE_PARENT_AGENT, "test-tenant-other", "example_read")
        is not None
    )
