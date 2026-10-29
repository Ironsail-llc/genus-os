"""Route the platform's connections through ``robothor_app`` with RLS on.

For integration tests that must reproduce what a deployment with row-level
security enforcing sees: a non-superuser role (migration 082), the REAL
``_apply_tenant_scope``, and a process bound to the platform tenant — a root
shell. Everything runs inside the ``db_conn`` transaction the integration
fixtures roll back, so nothing survives a test.

The same shape as ``crm/bridge/tests/test_tenant_switch_rls_integration.py``
(``app_role``), made importable from the ``robothor/`` suites. Import the
fixture by name: ``from tests.rls_app_role import app_role, rls_tenants``.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from typing import Any

import pytest


class AppRoleConn:
    """``db_conn`` seen through ``SET ROLE robothor_app``.

    Commit and rollback are no-ops so the fixture's rollback keeps isolation;
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
def rls_tenants(db_conn) -> dict[str, str]:
    """A child of the platform tenant, under a per-test id."""
    from robothor.constants import DEFAULT_TENANT

    suffix = uuid.uuid4().hex[:8]
    ids = {"root": DEFAULT_TENANT, "a": f"acme-child-a-{suffix}", "b": f"acme-child-b-{suffix}"}
    with db_conn.cursor() as cur:
        for key in ("a", "b"):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, %s)",
                (ids[key], f"Child {key}", DEFAULT_TENANT),
            )
    return ids


@pytest.fixture
def app_role(db_conn, monkeypatch):
    """Every connection the platform opens goes through ``robothor_app``, bound
    by the real ``_apply_tenant_scope`` to the process's tenant (the platform
    tenant) unless a ``tenant_scope``/``unbound_scope`` says otherwise."""
    from robothor.db import connection
    from tests import conftest_integration

    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "default")
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)

    @contextmanager
    def scoped(autocommit: bool = False):
        conn = AppRoleConn(db_conn)
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
    return scoped
