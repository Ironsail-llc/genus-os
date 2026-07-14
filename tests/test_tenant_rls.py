"""Tenant isolation must be enforced by the database, not by remembering a WHERE.

`robothor/crm/dal.py` filters by tenant (87 predicates). The bridge's
`crm_dal.py` does **not** (zero) — its read helpers take a bare id and no tenant
(`get_company(company_id)`), so the HTTP API can return any tenant's row by id.
Rewriting 2,071 lines of DAL is one answer; making Postgres refuse to serve the
row is the durable one: then a forgotten WHERE clause leaks nothing.

Two things have to be true for that to be real, and both are tested here:

1. The policy actually isolates (migration 081).
2. The app connects as a role RLS can constrain (migration 082). **A Postgres
   superuser bypasses RLS unconditionally** — and the engine connects as one
   today, which would have made the whole exercise theatre.
"""

from __future__ import annotations

import getpass
import os
import uuid
from pathlib import Path

import psycopg2
import pytest

pytestmark = pytest.mark.integration

MIGRATIONS = [
    "crm/migrations/081_tenant_rls_backstop.sql",
    "crm/migrations/082_tenant_rls_app_role.sql",
]


@pytest.fixture
def rls_db():
    """A scratch database with a tenant table, RLS applied, and the app role."""
    # Connect as the OS user (peer auth) unless a DSN is provided — CI supplies
    # ROBOTHOR_TEST_ADMIN_DSN; a dev box uses the local superuser.
    admin = os.environ.get("ROBOTHOR_TEST_ADMIN_DSN", f"dbname=postgres user={getpass.getuser()}")
    name = f"rls_test_{uuid.uuid4().hex[:8]}"

    conn = psycopg2.connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn.close()

    dsn = f"dbname={name} user={getpass.getuser()}"
    db = psycopg2.connect(dsn)
    db.autocommit = True
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE crm_tasks (id serial primary key, tenant_id text not null, title text)"
        )
        cur.execute(
            "INSERT INTO crm_tasks (tenant_id, title) VALUES "
            "('tenant-a','A secret'), ('tenant-b','B secret')"
        )
        for path in MIGRATIONS:
            cur.execute(Path(path).read_text())
    yield db, dsn

    db.close()
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    conn.close()


def _rows_as_app(db, tenant: str | None) -> list[str]:
    with db.cursor() as cur:
        cur.execute("SET ROLE robothor_app")
        if tenant is not None:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        cur.execute("SELECT tenant_id FROM crm_tasks ORDER BY tenant_id")
        rows = [r[0] for r in cur.fetchall()]
        cur.execute("RESET ROLE")
    return rows


def test_tenant_sees_only_its_own_rows(rls_db):
    db, _ = rls_db
    assert _rows_as_app(db, "tenant-a") == ["tenant-a"], (
        "tenant-a can see another tenant's rows — the RLS policy is not isolating"
    )
    assert _rows_as_app(db, "tenant-b") == ["tenant-b"]


def test_unscoped_connection_still_works(rls_db):
    """Legacy paths (migrations, psql, the CLI) must not break."""
    db, _ = rls_db
    assert sorted(_rows_as_app(db, None)) == ["tenant-a", "tenant-b"]


def test_a_superuser_bypasses_rls_entirely(rls_db):
    """The finding that makes the app-role migration necessary.

    If the engine connects as a superuser — it does today — RLS protects
    nothing, whatever ENABLE/FORCE say. This test documents that, so nobody
    "enables RLS" and believes they are isolated.
    """
    db, _ = rls_db
    with db.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', 'tenant-a', false)")
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is_super = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM crm_tasks")
        visible = cur.fetchone()[0]

    if is_super:
        assert visible == 2, (
            "a superuser is expected to bypass RLS — if this ever changes, the "
            "app-role migration's rationale should be revisited"
        )


def test_app_role_cannot_bypass_rls(rls_db):
    db, _ = rls_db
    with db.cursor() as cur:
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'robothor_app'")
        rolsuper, rolbypassrls = cur.fetchone()
    assert not rolsuper, "robothor_app must not be a superuser or RLS is void"
    assert not rolbypassrls, "robothor_app must not have BYPASSRLS or RLS is void"
