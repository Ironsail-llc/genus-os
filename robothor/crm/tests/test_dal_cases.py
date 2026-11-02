"""The case filters on ``list_tasks``: ``overdue`` and ``unowned``.

A case is a CRM task (``docs/CRM_DATA_FABRIC.md``, "Cases"). ``overdue`` keeps
tasks whose ``due_at`` has passed; ``unowned`` keeps tasks no agent owns or that
wait on a person (``requires_human``). Together they list what the
``crm.cases_overdue`` doctor check counts.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


def _mock_conn():
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


def _sql_of(**kwargs) -> str:
    conn, cur = _mock_conn()
    with patch("robothor.crm.dal.get_connection", return_value=conn):
        from robothor.crm.dal import list_tasks

        list_tasks(**kwargs)
    return " ".join(cur.execute.call_args[0][0].split())


class TestListTasksCaseFilters:
    def test_overdue_keeps_tasks_past_due(self) -> None:
        assert "due_at < NOW()" in _sql_of(overdue=True)

    def test_unowned_keeps_tasks_without_an_agent_or_waiting_on_a_person(self) -> None:
        assert "(assigned_to_agent IS NULL OR requires_human IS TRUE)" in _sql_of(unowned=True)

    def test_without_the_filters_the_query_is_unchanged(self) -> None:
        sql = _sql_of()
        assert "due_at < NOW()" not in sql
        assert "assigned_to_agent IS NULL" not in sql

    def test_the_filters_never_return_resolved_tasks(self) -> None:
        sql = _sql_of(overdue=True, unowned=True, exclude_resolved=False)
        assert "status != 'DONE'" in sql


@pytest.fixture
def db_conn(db_dsn):
    """Skip, rather than fail, when no integration database is reachable."""
    import psycopg2

    try:
        conn = psycopg2.connect(db_dsn)
    except psycopg2.Error as exc:
        pytest.skip(
            f"integration database unreachable ({type(exc).__name__}) — set ROBOTHOR_TEST_DB_DSN"
        )
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.integration
def test_overdue_unowned_returns_exactly_the_unowned_and_waiting_cases(
    db_cursor, db_conn, mock_get_connection
):
    """The mocked tests assert the SQL is issued; this asserts it selects the right rows."""
    from robothor.constants import DEFAULT_TENANT
    from robothor.crm.dal import list_tasks

    tenant = DEFAULT_TENANT
    rows = {
        "unowned": (None, False, "TODO", "-1 day"),
        "needs_person": ("example-agent", True, "TODO", "-1 day"),
        "owned": ("example-agent", False, "TODO", "-1 day"),
        "not_due": (None, False, "TODO", "+1 day"),
        "resolved": (None, False, "DONE", "-1 day"),
    }
    ids = {name: str(uuid.uuid4()) for name in rows}
    for name, (agent, requires_human, status, offset) in rows.items():
        db_cursor.execute(
            """INSERT INTO crm_tasks
                   (id, tenant_id, title, status, assigned_to_agent, requires_human, due_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW() + %s::interval)""",
            (ids[name], tenant, f"case {name}", status, agent, requires_human, offset),
        )

    found = {
        task["id"] for task in list_tasks(overdue=True, unowned=True, tenant_id=tenant, limit=500)
    }
    ours = found & set(ids.values())
    assert ours == {ids["unowned"], ids["needs_person"]}
