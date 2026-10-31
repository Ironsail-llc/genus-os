"""Real Postgres, real RLS, a non-superuser role: the binding actually isolates.

The unit tests prove the bridge SETS a scope; these prove the database then
refuses to serve another tenant's row. Every connection in these tests runs as
``robothor_app`` (migration 082) — a superuser bypasses RLS unconditionally,
and a test that connected as one would pass against no isolation at all.

All rows are written inside the per-test transaction that ``db_conn`` rolls
back, under unique tenant ids, so nothing survives the test.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.integration

KEY = "test-signing-key-at-least-32-bytes-long-xyz"


class _AppRoleConn:
    """``db_conn`` seen through ``SET ROLE robothor_app``.

    Commit and close are no-ops so the fixture's rollback keeps isolation;
    close restores the superuser role so the test can keep seeding.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        with real.cursor() as cur:
            cur.execute("SET ROLE robothor_app")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        with self._real.cursor() as cur:
            cur.execute("RESET ROLE")


@pytest.fixture
def tenants(db_conn) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    ids = {
        "parent": f"acme-parent-{suffix}",
        "a": f"acme-child-a-{suffix}",
        "b": f"acme-child-b-{suffix}",
    }
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)",
            (ids["parent"], "Parent"),
        )
        for key in ("a", "b"):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, %s)",
                (ids[key], f"Child {key}", ids["parent"]),
            )
    return ids


@pytest.fixture
def app_role(db_conn, monkeypatch):
    """Route every connection the platform opens through ``robothor_app``.

    ``get_connection`` is replaced by one that applies the REAL
    ``_apply_tenant_scope`` — the code under test — and the vault's raw
    ``psycopg2.connect`` returns the same transaction.
    """
    import psycopg2

    from robothor.db import connection
    from tests import conftest_integration

    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "default")
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @contextmanager
    def scoped(autocommit: bool = False):
        conn = _AppRoleConn(db_conn)
        try:
            connection._apply_tenant_scope(conn)  # type: ignore[arg-type]
            yield conn
        finally:
            conn.close()

    real = conftest_integration._current_holder.get("real")
    proxy = conftest_integration._session_fake_get_connection
    monkeypatch.setitem(conftest_integration._current_holder, "real", scoped)
    monkeypatch.setitem(conftest_integration._current_holder, "conn", None)
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith(("robothor.", "routers.", "crm.")):
            continue
        local = getattr(module, "get_connection", None)
        if local is not None and local in (real, proxy, connection.get_connection):
            monkeypatch.setattr(module, "get_connection", scoped)
    monkeypatch.setattr(connection, "get_connection", scoped)
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _AppRoleConn(db_conn))
    return scoped


def _seed_people(db_conn, tenants: dict[str, str]) -> dict[str, str]:
    ids = {}
    with db_conn.cursor() as cur:
        for key in ("a", "b"):
            pid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO crm_people (id, first_name, last_name, tenant_id) "
                "VALUES (%s, %s, %s, %s)",
                (pid, "Alice", key, tenants[key]),
            )
            ids[key] = pid
    return ids


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    # Acting tokens are honoured only while switching is enforced.
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    from robothor.auth import tokens
    from robothor.settings import reset_settings

    tokens.reset_signing_key_cache()
    reset_settings()
    yield
    tokens.reset_signing_key_cache()
    reset_settings()


@pytest.fixture
def forgetful_route():
    """A route that forgets its tenant WHERE — the leak RLS must stop."""
    from bridge_service import app

    from robothor.db.connection import get_connection

    holder: dict[str, list[str]] = {}

    def handler():
        from robothor.db import connection

        with connection.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM crm_people WHERE id = ANY(%s::uuid[]) ORDER BY tenant_id",
                (holder["ids"],),
            )
            return {"tenants": [row[0] for row in cur.fetchall()]}

    del get_connection
    before = list(app.router.routes)
    app.add_api_route("/api/_rls_probe", handler, methods=["GET"])
    try:
        yield holder
    finally:
        app.router.routes[:] = before


@pytest.mark.asyncio
async def test_a_child_token_reads_only_child_rows(
    test_client, db_conn, tenants, app_role, forgetful_route
):
    from robothor.auth import tokens

    people = _seed_people(db_conn, tenants)
    forgetful_route["ids"] = list(people.values())

    token = tokens.issue_access_token(
        "u-alice", tenants["a"], "admin", home_tenant_id=tenants["parent"]
    )
    r = await test_client.get("/api/_rls_probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json() == {"tenants": [tenants["a"]]}, (
        "a child token read another tenant's row through a query with no WHERE"
    )


@pytest.mark.asyncio
async def test_negative_control_unscoped_reads_both(
    test_client, db_conn, tenants, app_role, forgetful_route
):
    """Without a token there is no scope; with the default tenant bound, neither
    child row is visible. Under unbound_scope both are — proving the rows exist
    and the previous test is not passing on an empty table."""
    from robothor.db import connection

    people = _seed_people(db_conn, tenants)
    with connection.unbound_scope(), connection.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM crm_people WHERE id = ANY(%s::uuid[]) ORDER BY tenant_id",
            (list(people.values()),),
        )
        assert sorted(r[0] for r in cur.fetchall()) == sorted([tenants["a"], tenants["b"]])


def _seed_sso_account(db_conn, tenant: str) -> tuple[str, str]:
    issuer = "https://idp.example.com"
    subject = f"sub-{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, idp_issuer, idp_subject, "
            "display_name, role, status) VALUES (%s, %s, %s, %s, %s, 'member', 'active')",
            (tenant, f"alice-{subject}@example.com", issuer, subject, "Alice"),
        )
    return issuer, subject


def test_sso_lookup_finds_a_child_account(db_conn, tenants, app_role):
    from robothor.auth import accounts

    issuer, subject = _seed_sso_account(db_conn, tenants["a"])
    found = accounts.get_account_by_idp(issuer, subject)
    assert found is not None and found["tenant_id"] == tenants["a"]


def test_jit_provision_signs_in_a_child_account(db_conn, tenants, app_role):
    """Without the unbound scope the lookup misses (RLS hides the row), the
    INSERT collides on the platform-wide (issuer, subject) key, and sign-in
    500s with a UniqueViolation."""
    from robothor.auth import accounts

    issuer, subject = _seed_sso_account(db_conn, tenants["a"])
    account = accounts.jit_provision(
        issuer=issuer,
        subject=subject,
        email=f"alice-{subject}@example.com",
        display_name="Alice",
        tenant_id="default",
    )
    assert account["tenant_id"] == tenants["a"]


def test_the_raw_lookup_misses_without_the_unbound_scope(db_conn, tenants, app_role):
    """The negative control for the two tests above."""
    issuer, subject = _seed_sso_account(db_conn, tenants["a"])
    with app_role() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM user_accounts WHERE idp_issuer = %s AND idp_subject = %s",
            (issuer, subject),
        )
        assert cur.fetchone() is None


def test_vault_reads_inside_a_child_scope_see_only_that_tenant(db_conn, tenants, app_role):
    from robothor.db.connection import tenant_scope
    from robothor.vault import dal

    with db_conn.cursor() as cur:
        for key in ("a", "b"):
            cur.execute(
                "INSERT INTO vault_secrets (tenant_id, key, encrypted_value, category) "
                "VALUES (%s, %s, %s, 'credential')",
                (tenants[key], f"svc/{key}-token", b"x"),
            )

    with tenant_scope(tenants["a"]):
        assert dal.list_keys() == ["svc/a-token"]


def test_an_explicit_vault_tenant_wins_over_the_ambient_scope(
    db_conn, tenants, app_role, monkeypatch
):
    """Instance credentials (the SIEM webhook, provider keys, the Telegram
    token) are asked for with ``tenant_id=DEFAULT_TENANT`` explicitly, and
    must still be found inside a child-scoped request or a sandboxed run."""
    import robothor.vault as vault
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import tenant_scope
    from robothor.vault import dal

    key = f"svc/instance-{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        for tenant, value in ((DEFAULT_TENANT, b"root"), (tenants["a"], b"child")):
            cur.execute(
                "INSERT INTO vault_secrets (tenant_id, key, encrypted_value, category) "
                "VALUES (%s, %s, %s, 'credential')",
                (tenant, key, value),
            )
    monkeypatch.setattr(vault, "get_master_key", lambda: b"0" * 32)
    monkeypatch.setattr(dal, "decrypt", lambda blob, master: bytes(blob).decode())

    with tenant_scope(tenants["a"]):
        assert vault.get(key, tenant_id=DEFAULT_TENANT) == "root"
        assert vault.get(key) == "child"
