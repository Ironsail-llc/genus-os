"""``unbound_scope()``: the one way a sign-in may look across tenants.

The bridge now binds every authenticated request to its token's tenant. A
sign-in has no token yet, and the account it is looking for may live in any
tenant of the hierarchy -- ``(idp_issuer, idp_subject)`` is unique platform
wide -- so the lookup must run on the permissive branch of the migration-081
policy (``app.tenant_id = ''``), and nothing else may.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.db import connection


class _Cursor:
    def __init__(self, sink: list[tuple[str, Any]]) -> None:
        self._sink = sink

    def execute(self, sql: str, params: Any = None) -> None:
        self._sink.append((sql, params))

    def fetchone(self) -> tuple[bool]:
        return (False,)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.executed)


def _bound_tenant(conn: _Conn) -> Any:
    binds = [p for sql, p in conn.executed if "app.tenant_id" in sql]
    assert binds, "no tenant binding was applied"
    return binds[-1][0]


@pytest.fixture
def rls_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "acme-parent")
    monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)


def test_unbound_scope_binds_the_permissive_empty_tenant(rls_on: None) -> None:
    conn = _Conn()
    with connection.unbound_scope():
        connection._apply_tenant_scope(conn)  # type: ignore[arg-type]
    assert _bound_tenant(conn) == ""


def test_unbound_scope_overrides_an_enclosing_tenant_scope(rls_on: None) -> None:
    conn = _Conn()
    with connection.tenant_scope("acme-child-a"), connection.unbound_scope():
        connection._apply_tenant_scope(conn)  # type: ignore[arg-type]
    assert _bound_tenant(conn) == ""


def test_the_enclosing_scope_returns_on_exit(rls_on: None) -> None:
    conn = _Conn()
    with connection.tenant_scope("acme-child-a"):
        with connection.unbound_scope():
            pass
        connection._apply_tenant_scope(conn)  # type: ignore[arg-type]
    assert _bound_tenant(conn) == "acme-child-a"


def test_unbound_is_not_reported_as_a_tenant(rls_on: None) -> None:
    """Callers that default a tenant from the scope (the vault, the runner)
    must fall back to their own default, never write under a sentinel."""
    with connection.unbound_scope():
        assert connection.current_tenant_scope() is None
        assert connection.effective_tenant() == ""


def test_without_any_scope_the_env_tenant_binds(rls_on: None) -> None:
    conn = _Conn()
    connection._apply_tenant_scope(conn)  # type: ignore[arg-type]
    assert _bound_tenant(conn) == "acme-parent"


def test_both_widenings_share_one_docstring() -> None:
    """The two helpers differ in scope (a transaction vs. a block of
    checkouts) and in who may call them; one explanation keeps them honest."""
    assert connection.unbound_scope.__doc__
    assert connection.unbound_scope.__doc__ == connection.read_every_tenant_in_transaction.__doc__
