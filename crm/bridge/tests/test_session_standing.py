"""A verified token is not enough: the account must still stand where it says.

Two defects found in end-to-end QA with three engines and RLS on:

* **An old access token outlived an account move.** After an account was
  moved from ``acme-child-a`` to ``acme-child-b`` (sessions revoked, refresh
  refused), its unexpired access token kept reading child-a for up to fifteen
  minutes. A human token is now honoured only while its account still lives in
  the token's ``home_tid`` and is active — checked against the database through
  a per-process cache with a 30-second TTL, so it costs one SELECT per user per
  30 s, and dropped at once when the bridge itself changes the account.
* **Acting tokens outlived the switch flag.** With ``GENUS_TENANT_SWITCH_MODE``
  turned back to ``off`` (or ``observe``), acting tokens issued earlier were
  still honoured. An acting token is now refused unless the mode is
  ``enforce``; a home token is unaffected.

The bridge suite's autouse ``sessions_stand`` fixture stubs the standing check
for every other test (their tokens name accounts no database holds); the tests
here put the real one back.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"
ALICE = "00000000-0000-4000-8000-0000000000a1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, real_session_standing):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    from robothor.auth import session_standing, tokens
    from robothor.settings import reset_settings

    tokens.reset_signing_key_cache()
    reset_settings()
    session_standing.forget_all()
    yield
    session_standing.forget_all()
    tokens.reset_signing_key_cache()
    reset_settings()


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        from robothor.settings import reset_settings

        monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", value)
        reset_settings()

    return set_mode


def _account(tenant: str, status: str = "active") -> dict:
    return {"id": ALICE, "tenant_id": tenant, "status": status, "role": "admin"}


def _client(tenant: str, role: str = "admin", *, home: str | None = None, service=False):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    if service:
        token = tokens.issue_service_token("email-classifier", tenant, agent_id="email-classifier")
    else:
        token = tokens.issue_access_token(ALICE, tenant, role, home_tenant_id=home)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


PROBE = "/api/tenants/accessible"


def _probe(client):
    with (
        patch("robothor.engine.permissions.resolve_accessible_tenants", return_value=(CHILD_A,)),
        patch("routers.tenants.crm_dal.get_tenant", return_value=None),
    ):
        return client.get(PROBE)


# ─── D5: the account must still stand in its home tenant ──────────────


class TestAccountStanding:
    def test_an_account_still_in_its_home_tenant_passes(self):
        with patch("robothor.auth.accounts.get_account_by_id", return_value=_account(CHILD_A)):
            assert _probe(_client(CHILD_A)).status_code == 200

    def test_an_account_moved_to_another_tenant_is_refused(self):
        with patch("robothor.auth.accounts.get_account_by_id", return_value=_account(CHILD_B)):
            response = _probe(_client(CHILD_A))
        assert response.status_code == 401
        assert response.json() == {"error": "session no longer valid"}

    def test_a_disabled_account_is_refused(self):
        with patch(
            "robothor.auth.accounts.get_account_by_id",
            return_value=_account(CHILD_A, status="disabled"),
        ):
            assert _probe(_client(CHILD_A)).status_code == 401

    def test_a_vanished_account_is_refused(self):
        """Under RLS a moved account is invisible from its old home: no row."""
        with patch("robothor.auth.accounts.get_account_by_id", return_value=None):
            assert _probe(_client(CHILD_A)).status_code == 401

    def test_the_lookup_runs_in_the_home_tenant_not_the_acting_one(self, mode):
        mode("enforce")
        from robothor.db.connection import current_tenant_scope

        seen: list[str | None] = []

        def _lookup(user_id):
            seen.append(current_tenant_scope())
            return _account("acme-parent")

        with patch("robothor.auth.accounts.get_account_by_id", side_effect=_lookup):
            assert _probe(_client(CHILD_A, home="acme-parent")).status_code == 200
        assert seen == ["acme-parent"]

    def test_one_lookup_per_user_per_ttl(self, monkeypatch):
        from robothor.auth import session_standing

        clock = {"now": 1000.0}
        monkeypatch.setattr(session_standing, "_now", lambda: clock["now"])
        with patch(
            "robothor.auth.accounts.get_account_by_id", return_value=_account(CHILD_A)
        ) as lookup:
            client = _client(CHILD_A)
            for _ in range(3):
                assert _probe(client).status_code == 200
            assert lookup.call_count == 1
            clock["now"] += session_standing.TTL_SECONDS + 1
            assert _probe(client).status_code == 200
            assert lookup.call_count == 2

    def test_forgetting_an_account_rechecks_it_at_once(self):
        from robothor.auth import session_standing

        with patch(
            "robothor.auth.accounts.get_account_by_id", return_value=_account(CHILD_A)
        ) as lookup:
            client = _client(CHILD_A)
            assert _probe(client).status_code == 200
            lookup.return_value = _account(CHILD_B)
            session_standing.forget(ALICE)
            assert _probe(client).status_code == 401

    def test_a_service_token_is_not_looked_up(self):
        with patch("robothor.auth.accounts.get_account_by_id") as lookup:
            _client(CHILD_A, service=True).get("/api/notifications")
        lookup.assert_not_called()

    def test_disabling_an_account_in_the_helm_forgets_its_standing(self):
        """The bridge's own account change must not wait out the TTL."""
        import inspect

        from routers import users

        assert "session_standing.forget(" in inspect.getsource(users.update_user)


# ─── D8: acting tokens need the switch to be on ───────────────────────


class TestActingTokensNeedEnforce:
    @pytest.fixture(autouse=True)
    def _standing(self):
        with patch(
            "robothor.auth.accounts.get_account_by_id", return_value=_account("acme-parent")
        ):
            yield

    @pytest.mark.parametrize("value", ["off", "observe", ""])
    def test_an_acting_token_is_refused_unless_enforce(self, mode, value):
        mode(value)
        response = _probe(_client(CHILD_A, home="acme-parent"))
        assert response.status_code == 403
        assert response.json() == {"error": "tenant switching disabled"}

    def test_an_acting_token_is_honoured_under_enforce(self, mode):
        mode("enforce")
        assert _probe(_client(CHILD_A, home="acme-parent")).status_code == 200

    @pytest.mark.parametrize("value", ["off", "observe"])
    def test_a_home_token_is_unaffected(self, mode, value):
        mode(value)
        assert _probe(_client("acme-parent")).status_code == 200


# ─── Real Postgres, real RLS ──────────────────────────────────────────


def _rls_fixtures():
    import importlib.util

    path = Path(__file__).with_name("test_tenant_switch_rls_integration.py")
    spec = importlib.util.spec_from_file_location("_bridge_rls_fixtures_standing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _rls_fixtures()
app_role = _shared.app_role
tenants = _shared.tenants


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_old_token_is_refused_once_the_account_moves(
    test_client, db_conn, tenants, app_role, monkeypatch
):
    import uuid

    from robothor.auth import session_standing, tokens

    account_id = str(uuid.uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, %s, 'Alice', 'admin', 'active')",
            (account_id, tenants["a"], f"alice-{account_id[:8]}@example.com"),
        )
    clock = {"now": 1000.0}
    monkeypatch.setattr(session_standing, "_now", lambda: clock["now"])
    headers = {
        "Authorization": "Bearer " + tokens.issue_access_token(account_id, tenants["a"], "admin")
    }

    before = await test_client.get("/api/auth/me", headers=headers)
    assert before.status_code == 200, before.text

    # The move, as `genus tenant move-account` makes it: another process, so
    # nothing tells this bridge — the TTL is what bounds the window.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE user_accounts SET tenant_id = %s WHERE id = %s", (tenants["b"], account_id)
        )
    clock["now"] += session_standing.TTL_SECONDS + 1

    after = await test_client.get("/api/auth/me", headers=headers)
    assert after.status_code == 401
    assert after.json() == {"error": "session no longer valid"}


# ─── D7: who acted, and in which tenant ───────────────────────────────


def test_the_tenant_switch_audit_row_is_filed_under_the_target_tenant(mode, monkeypatch):
    """So a child's admin can see who entered their tenant."""
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens
    from robothor.federation import rls_gate

    mode("enforce")
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: True)
    monkeypatch.setattr(rls_gate, "_connection_is_superuser", lambda: False)
    owner = {**_account("acme-parent"), "role": "owner", "email": "alice@example.com"}
    token = tokens.issue_access_token(ALICE, "acme-parent", "owner")
    with (
        patch("robothor.auth.accounts.get_account_by_id", return_value=owner),
        patch(
            "robothor.engine.permissions.resolve_accessible_tenants",
            return_value=("acme-parent", CHILD_A),
        ),
        patch("routers._audit.log_event") as log_event,
    ):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/auth/switch-tenant",
            json={"tenant": CHILD_A},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    switch = [c for c in log_event.call_args_list if c.args and c.args[0] == "tenant.switch"]
    assert switch, log_event.call_args_list
    assert switch[-1].kwargs["tenant_id"] == CHILD_A
    assert switch[-1].kwargs["details"]["home_tenant_id"] == "acme-parent"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_crm_write_while_acting_is_audited_as_the_owner_in_the_child(
    test_client, db_conn, tenants, app_role, monkeypatch
):
    from robothor.audit import logger as audit
    from robothor.auth import tokens
    from robothor.settings import reset_settings

    migration = Path(__file__).resolve().parents[2] / "migrations" / "159_audit_log_tenant.sql"
    with db_conn.cursor() as cur:
        cur.execute(migration.read_text(encoding="utf-8"))

    class _NoCommit:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            return None

    monkeypatch.setattr(audit, "_release_connection", lambda conn: None)
    audit.set_connection_factory(lambda: _NoCommit(db_conn))
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    reset_settings()
    owner_id = "00000000-0000-4000-8000-0000000000f1"
    # The owner's account exists in its HOME tenant: the session check reads it.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, 'owner-qa@example.com', 'Owner', 'owner', 'active')",
            (owner_id, tenants["parent"]),
        )
    token = tokens.issue_access_token(
        owner_id, tenants["a"], "owner", home_tenant_id=tenants["parent"]
    )
    try:
        response = await test_client.post(
            "/api/tasks",
            json={"title": "Check the invoices"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["id"]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT actor, user_id, tenant_id FROM audit_log "
                "WHERE event_type = 'crm.create' AND target = %s",
                (f"task:{task_id}",),
            )
            row = cur.fetchone()
    finally:
        audit.reset_connection_factory()
    assert row is not None
    actor, user_id, tenant_id = row
    assert user_id == owner_id
    assert actor == owner_id
    assert tenant_id == tenants["a"]
