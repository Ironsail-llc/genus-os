"""Fixtures for the rooms data layer against real Postgres.

The data layer opens and commits its own transactions, and two of its promises
are about concurrent callers, so a per-test rollback fixture cannot observe
them. Each DAL call here gets its own committed connection to the ``*_test``
database named by ``ROBOTHOR_TEST_DB_DSN``; every row is written under tenant
ids suffixed per test and removed afterwards.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

PG_DSN = os.environ.get(
    "ROBOTHOR_TEST_DB_DSN",
    "dbname=robothor_test user=robothor host=/var/run/postgresql",
)

ROOM_TABLES = ("room_tasks", "room_editions", "room_revisions", "room_members", "rooms")


def connect_test_db() -> Any:
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(PG_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no test database reachable at ROBOTHOR_TEST_DB_DSN: {exc}")
    if not conn.get_dsn_parameters().get("dbname", "").endswith("_test"):
        conn.close()
        pytest.skip("ROBOTHOR_TEST_DB_DSN must name a *_test database")
    return conn


@contextlib.contextmanager
def committed_connection(autocommit: bool = False, read_only: bool = False) -> Iterator[Any]:
    """The shape of ``robothor.db.connection.get_connection``, unpooled."""
    conn = connect_test_db()
    conn.autocommit = autocommit or read_only
    try:
        yield conn
        if not conn.autocommit:
            conn.commit()
    except BaseException:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        conn.close()


@pytest.fixture
def rooms_db(monkeypatch) -> Iterator[SimpleNamespace]:
    """Two tenants, two users each, and a cursor for seeding and inspecting."""
    from robothor.rooms import dal

    monkeypatch.setattr(dal, "get_connection", committed_connection)
    sfx = uuid.uuid4().hex[:8]
    tenant, other = f"test-tenant-{sfx}", f"test-tenant-other-{sfx}"
    conn = connect_test_db()
    conn.autocommit = True
    cur = conn.cursor()
    for tid in (tenant, other):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tid, "Test tenant")
        )
    try:
        yield SimpleNamespace(
            tenant=tenant,
            other=other,
            alice=str(uuid.uuid4()),
            bob=str(uuid.uuid4()),
            carol=str(uuid.uuid4()),
            cur=cur,
        )
    finally:
        tenants = (tenant, other)
        cur.execute(
            "DELETE FROM chat_sessions WHERE tenant_id = ANY(%s)",
            (list(tenants),),
        )
        # A published edition's suggestions become CRM tasks with a history row
        # (robothor/rooms/tasks.py); the history goes first.
        cur.execute("DELETE FROM crm_task_history WHERE tenant_id = ANY(%s)", (list(tenants),))
        cur.execute("DELETE FROM crm_tasks WHERE tenant_id = ANY(%s)", (list(tenants),))
        for table in ROOM_TABLES:
            with contextlib.suppress(Exception):
                cur.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",  # noqa: S608
                    (list(tenants),),
                )
        cur.execute("DELETE FROM crm_tenants WHERE id = ANY(%s)", (list(tenants),))
        conn.close()
