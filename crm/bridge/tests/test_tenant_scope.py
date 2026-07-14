"""The bridge must bind its connections to the tenant, like the engine does.

Migration 081 puts RLS on all 59 tenant tables with a policy keyed on the
`app.tenant_id` GUC, and `robothor/db/connection.py` sets that GUC on every
connection it hands out. The bridge does not use that module: `crm_dal._conn()`
calls `psycopg2.connect(config.PG_DSN)` directly.

That matters more here than anywhere else. The bridge is the *API* — the actual
cross-tenant leak surface — and `crm_dal.py` has **zero** tenant predicates in
2,000+ lines. It relies entirely on the database to scope rows. A connection
that never sets `app.tenant_id` gets the permissive branch of the policy and
sees every tenant's data.

So RLS could be fully enabled, correct, and enforced, and the bridge would
still serve another tenant's row by id.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeCursor:
    def __init__(self, sink: list[tuple[str, Any]]) -> None:
        self._sink = sink

    def execute(self, sql: str, params: Any = None) -> None:
        self._sink.append((sql, params))

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.executed)


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    from crm.bridge import crm_dal

    conn = _FakeConn()
    monkeypatch.setattr(crm_dal.psycopg2, "connect", lambda *a, **k: conn)
    return conn


class TestBridgeBindsTheTenant:
    def test_conn_sets_app_tenant_id_when_rls_is_enabled(
        self, monkeypatch: pytest.MonkeyPatch, fake_connect: _FakeConn
    ) -> None:
        from crm.bridge import crm_dal

        monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-corp")

        crm_dal._conn()

        statements = [sql for sql, _ in fake_connect.executed]
        assert any("set_config" in s and "app.tenant_id" in s for s in statements), (
            "the bridge never binds its connection to a tenant — RLS falls back to "
            "the permissive branch and the API serves every tenant's rows"
        )
        params = [p for sql, p in fake_connect.executed if "app.tenant_id" in sql]
        assert params[0] == ("acme-corp",)

    def test_falls_back_to_default_tenant(
        self, monkeypatch: pytest.MonkeyPatch, fake_connect: _FakeConn
    ) -> None:
        """Same precedence as the engine: ROBOTHOR_TENANT_ID, then the default."""
        from crm.bridge import crm_dal

        monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
        monkeypatch.delenv("ROBOTHOR_TENANT_ID", raising=False)
        monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "robothor-primary")

        crm_dal._conn()

        params = [p for sql, p in fake_connect.executed if "app.tenant_id" in sql]
        assert params and params[0] == ("robothor-primary",)

    def test_no_scoping_when_rls_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch, fake_connect: _FakeConn
    ) -> None:
        """Off by default — the flag is the rollback lever."""
        from crm.bridge import crm_dal

        monkeypatch.delenv("ROBOTHOR_RLS_ENABLED", raising=False)
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-corp")

        crm_dal._conn()

        assert not [s for s, _ in fake_connect.executed if "app.tenant_id" in s]

    def test_a_failed_bind_is_loud_not_silent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A connection that silently forgets its tenant has no isolation at all."""
        from crm.bridge import crm_dal

        class _Boom(_FakeConn):
            def cursor(self) -> _FakeCursor:
                raise RuntimeError("no cursor for you")

        monkeypatch.setattr(crm_dal.psycopg2, "connect", lambda *a, **k: _Boom())
        monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-corp")

        with caplog.at_level("ERROR"):
            crm_dal._conn()

        assert any("tenant" in r.message.lower() for r in caplog.records), (
            "a tenant bind that fails quietly is worse than no RLS — it looks enabled"
        )
