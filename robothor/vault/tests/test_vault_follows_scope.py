"""The vault follows the request's tenant scope, and is RLS-bound like the rest.

Before this, every vault function defaulted ``tenant_id`` to ``DEFAULT_TENANT``
and ``dal._get_conn()`` opened a raw psycopg2 connection that never bound
``app.tenant_id``. A request acting in a child tenant therefore read and wrote
the ROOT tenant's credentials, and with RLS on the vault was the one table the
database could not protect.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import tenant_scope, unbound_scope


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> _Conn:
    import psycopg2

    fake = _Conn()
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: fake)
    return fake


def _tenant_params(conn: _Conn) -> list[Any]:
    return [p[0] for sql, p in conn.executed if "vault_secrets" in sql and p]


def _binds(conn: _Conn) -> list[Any]:
    return [p[0] for sql, p in conn.executed if "app.tenant_id" in sql]


def test_get_defaults_to_the_enclosing_scope(conn: _Conn) -> None:
    from robothor.vault import dal

    with tenant_scope("acme-child-a"):
        dal.get_secret("k", b"0" * 32)
    assert _tenant_params(conn) == ["acme-child-a"]


def test_get_without_a_scope_still_defaults_to_the_default_tenant(conn: _Conn) -> None:
    from robothor.vault import dal

    dal.get_secret("k", b"0" * 32)
    assert _tenant_params(conn) == [DEFAULT_TENANT]


def test_unbound_scope_is_not_a_tenant(conn: _Conn) -> None:
    from robothor.vault import dal

    with unbound_scope():
        dal.list_keys()
    assert _tenant_params(conn) == [DEFAULT_TENANT]


def test_an_explicit_tenant_wins_over_the_scope(conn: _Conn) -> None:
    from robothor.vault import dal

    with tenant_scope("acme-child-a"):
        dal.count_secrets(tenant_id="acme-child-b")
    assert _tenant_params(conn) == ["acme-child-b"]


@pytest.mark.parametrize(
    "call",
    [
        lambda dal: dal.list_keys(),
        lambda dal: dal.count_secrets(),
        lambda dal: dal.delete_secret("k"),
        lambda dal: dal.get_secrets_updated_at(["k"]),
        lambda dal: dal.export_secrets(b"0" * 32),
    ],
)
def test_every_reader_defaults_to_the_scope(conn: _Conn, call: Any) -> None:
    from robothor.vault import dal

    with tenant_scope("acme-child-a"):
        call(dal)
    assert _tenant_params(conn) == ["acme-child-a"]


def test_set_writes_under_the_scope(conn: _Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    from robothor.vault import dal

    monkeypatch.setattr(dal, "encrypt", lambda value, key: b"x")
    with tenant_scope("acme-child-a"):
        dal.set_secret("k", "v", b"0" * 32)
    assert _tenant_params(conn) == ["acme-child-a"]


def test_the_dal_connection_is_rls_bound(conn: _Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    from robothor.vault import dal

    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    with tenant_scope("acme-child-a"):
        dal.list_keys()
    assert _binds(conn) == ["acme-child-a"], (
        "the vault's connection never bound app.tenant_id — RLS cannot protect it"
    )


def test_public_api_passes_the_scope_through(conn: _Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    import robothor.vault as vault

    monkeypatch.setattr(vault, "get_master_key", lambda: b"0" * 32)
    with tenant_scope("acme-child-a"):
        vault.get("k")
        vault.list()
    assert _tenant_params(conn) == ["acme-child-a", "acme-child-a"]


def test_an_explicit_tenant_binds_the_connection_to_that_tenant(
    conn: _Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit wins over ambient for the RLS binding too, or the WHERE would
    name a row the binding hides (the instance SIEM webhook inside a child
    request, provider keys inside a sandboxed run)."""
    from robothor.vault import dal

    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
    with tenant_scope("acme-child-a"):
        dal.list_keys(tenant_id=DEFAULT_TENANT)
    assert _binds(conn) == [DEFAULT_TENANT]
    assert _tenant_params(conn) == [DEFAULT_TENANT]
