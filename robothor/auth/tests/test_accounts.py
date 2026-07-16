"""accounts DAL: JIT provisioning paths + session revoke (DB mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.auth import accounts


def _mock_conn(fetchone_seq, rowcount=1):
    """A get_connection() context manager whose single cursor returns
    ``fetchone_seq`` across successive fetchone() calls."""
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_seq)
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_jit_provision_creates_new_account():
    # idp lookup -> None; email lookup -> None; INSERT RETURNING -> new row.
    new_row = {
        "id": "uid-1",
        "email": "alice@example.com",
        "role": "member",
        "tenant_id": "default",
    }
    conn, cur = _mock_conn([None, None, new_row])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://idp",
            subject="sub-1",
            email="alice@example.com",
            display_name="Ann",
        )
    assert out["id"] == "uid-1"
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "INSERT INTO user_accounts" in sql
    conn.commit.assert_called()


def test_jit_provision_never_links_privileged_account_by_email():
    # A verified email is not sufficient to bind an SSO subject to an existing
    # account, especially an administrator account. (Trailing None: the lookup
    # for an armed binding grant finds nothing.)
    existing = {
        "id": "uid-2",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
        "idp_issuer": None,
    }
    conn, cur = _mock_conn([None, existing, None, None])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.AccountBindingRequiredError):
            accounts.jit_provision(
                issuer="https://idp",
                subject="attacker-subject",
                email="owner@example.com",
                display_name="Mallory",
            )
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE user_accounts" not in sql
    assert "INSERT INTO user_accounts" not in sql
    conn.commit.assert_not_called()


@pytest.mark.parametrize("status", ["disabled", "invited", "pending", ""])
def test_jit_provision_rejects_non_active_bound_account(status):
    existing = {
        "id": "uid-disabled",
        "email": "disabled@example.com",
        "role": "member",
        "status": status,
    }
    conn, cur = _mock_conn([existing])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.AccountInactiveError):
            accounts.jit_provision(
                issuer="https://idp",
                subject="bound-subject",
                email="disabled@example.com",
                display_name="Disabled",
            )
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "last_login_at = NOW()" not in sql
    conn.commit.assert_not_called()


def test_jit_provision_returns_existing_idp_account():
    # idp lookup -> existing row; _touch_login UPDATE RETURNING -> same row.
    existing = {
        "id": "uid-3",
        "email": "carol@example.com",
        "role": "owner",
        "status": "active",
    }
    conn, cur = _mock_conn([existing, existing])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://idp",
            subject="sub-3",
            email="carol@example.com",
            display_name="Cy",
        )
    assert out["id"] == "uid-3"
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "INSERT INTO user_accounts" not in sql  # not re-created
    assert "last_login_at = NOW()" in sql


def test_jit_provision_refuses_privileged_default_role():
    with pytest.raises(accounts.UnsafeProvisioningRoleError):
        accounts.jit_provision(
            issuer="https://idp",
            subject="sub-admin",
            email="admin@example.com",
            display_name="Admin",
            default_role="admin",
        )


def test_create_binding_grant_inserts_row_and_supersedes_pending():
    target = {"id": "uid-owner", "status": "active", "idp_issuer": None}
    row = {"id": "grant-1", "email": "owner@example.com", "tenant_id": "default"}
    conn, cur = _mock_conn([target, row])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.create_binding_grant(
            email="Owner@Example.com", ttl_seconds=900, reason="initial owner binding"
        )
    assert out == row
    statements = [str(c[0][0]) for c in cur.execute.call_args_list]
    sql = " ".join(statements)
    assert "INSERT INTO sso_binding_grants" in sql
    assert "RETURNING *" in sql
    # Re-arming replaces: any still-pending grant for the email is revoked in
    # the same transaction, so at most one grant is ever live per account.
    assert any("UPDATE sso_binding_grants" in s and "revoked_at = NOW()" in s for s in statements)
    # email is normalized like every other accounts entry point
    params = cur.execute.call_args_list[0][0][1]
    assert "owner@example.com" in params
    conn.commit.assert_called_once()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (None, "no account"),
        ({"id": "u", "status": "active", "idp_issuer": "https://idp"}, "already bound"),
        ({"id": "u", "status": "disabled", "idp_issuer": None}, "not active"),
    ],
)
def test_create_binding_grant_rejects_unusable_target(target, message):
    # A grant that can never fire (typo'd email, bound or inactive account)
    # must fail loudly at arm time, not silently no-op at sign-in time.
    conn, cur = _mock_conn([target])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.GrantTargetError, match=message):
            accounts.create_binding_grant(email="owner@example.com", ttl_seconds=900)
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "INSERT INTO sso_binding_grants" not in sql
    conn.commit.assert_not_called()


def test_consume_binding_grant_is_single_atomic_update():
    # Consume must be one UPDATE ... RETURNING, not read-then-write: two
    # concurrent sign-ins must not both spend the same grant.
    grant = {"id": "grant-1"}
    cur = MagicMock()
    cur.fetchone.return_value = grant
    assert (
        accounts._consume_binding_grant(cur, "default", "owner@example.com", "https://idp") == grant
    )
    assert cur.execute.call_count == 1
    sql = str(cur.execute.call_args[0][0])
    assert "UPDATE sso_binding_grants" in sql
    assert "used_at IS NULL" in sql
    assert "revoked_at IS NULL" in sql
    assert "expires_at > NOW()" in sql
    # Issuer-pinned grants only match their IdP; unpinned grants match any.
    assert "issuer IS NULL OR issuer = %s" in sql
    assert "RETURNING *" in sql
    assert "https://idp" in cur.execute.call_args[0][1]


def test_jit_provision_binds_existing_account_with_active_grant():
    # idp lookup -> None; email lookup -> unbound active owner; grant consume
    # -> grant row; account UPDATE RETURNING -> bound row.
    existing = {
        "id": "uid-owner",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
        "idp_issuer": None,
    }
    bound = {**existing, "idp_issuer": "https://team.example", "idp_subject": "sub-9"}
    grant = {"id": "grant-1", "email": "owner@example.com"}
    conn, cur = _mock_conn([None, existing, grant, bound])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://team.example",
            subject="sub-9",
            email="owner@example.com",
            display_name="Owner",
        )
    assert out == bound
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE sso_binding_grants" in sql
    assert "UPDATE user_accounts" in sql
    assert "SET idp_issuer" in sql.replace("\n", " ").replace("  ", " ")
    assert "idp_issuer IS NULL" in sql  # never overwrites an existing binding
    assert "INSERT INTO user_accounts" not in sql  # bound, not re-created
    conn.commit.assert_called_once()  # one transaction, no partial state


def test_jit_provision_without_grant_still_raises_binding_required():
    existing = {
        "id": "uid-owner",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
        "idp_issuer": None,
    }
    # No grant, and the concurrent-winner recheck finds nothing either.
    conn, cur = _mock_conn([None, existing, None, None])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.AccountBindingRequiredError):
            accounts.jit_provision(
                issuer="https://team.example",
                subject="sub-9",
                email="owner@example.com",
                display_name="Owner",
            )
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE user_accounts" not in sql
    conn.commit.assert_not_called()


def test_jit_provision_grant_race_loser_still_signs_in():
    # Two concurrent sign-ins from the SAME identity: the loser finds the grant
    # already consumed, but the account is now bound to exactly this
    # (issuer, subject) — that's a successful sign-in, not an error.
    bound = {
        "id": "uid-owner",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
        "idp_issuer": "https://team.example",
        "idp_subject": "sub-9",
    }
    unbound = {**bound, "idp_issuer": None, "idp_subject": None}
    conn, cur = _mock_conn([None, unbound, None, bound, bound])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://team.example",
            subject="sub-9",
            email="owner@example.com",
            display_name="Owner",
        )
    assert out == bound


def test_jit_provision_grant_not_burned_for_inactive_account():
    # An inactive email match fails BEFORE the grant lookup, so an armed grant
    # survives for when the account is re-activated.
    existing = {
        "id": "uid-owner",
        "email": "owner@example.com",
        "role": "owner",
        "status": "invited",
        "idp_issuer": None,
    }
    conn, cur = _mock_conn([None, existing])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.AccountBindingRequiredError):
            accounts.jit_provision(
                issuer="https://team.example",
                subject="sub-9",
                email="owner@example.com",
                display_name="Owner",
            )
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE sso_binding_grants" not in sql
    conn.commit.assert_not_called()


def test_jit_provision_grant_never_rebinds_bound_account():
    # An account already bound to some (issuer, subject) is never re-bound via
    # the email path — not even with a grant armed.
    existing = {
        "id": "uid-owner",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
        "idp_issuer": "https://original-idp.example",
    }
    conn, cur = _mock_conn([None, existing])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        with pytest.raises(accounts.AccountBindingRequiredError):
            accounts.jit_provision(
                issuer="https://team.example",
                subject="sub-9",
                email="owner@example.com",
                display_name="Owner",
            )
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE sso_binding_grants" not in sql
    conn.commit.assert_not_called()


def test_revoke_binding_grant_true_then_false():
    conn, cur = _mock_conn([], rowcount=1)
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        assert accounts.revoke_binding_grant("grant-1") is True
    conn2, cur2 = _mock_conn([], rowcount=0)
    with patch("robothor.auth.accounts.get_connection", return_value=conn2):
        assert accounts.revoke_binding_grant("grant-2") is False


def test_consume_active_session_is_atomic():
    row = {"id": "session-1", "user_id": "uid-1"}
    conn, cur = _mock_conn([row])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        assert accounts.consume_active_session("hash-1") == row
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE user_sessions" in sql
    assert "revoked_at IS NULL" in sql
    assert "RETURNING *" in sql
    conn.commit.assert_called_once()


def test_revoke_session_true_then_false():
    conn, cur = _mock_conn([], rowcount=1)
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        assert accounts.revoke_session("hash-1") is True
    conn2, cur2 = _mock_conn([], rowcount=0)
    with patch("robothor.auth.accounts.get_connection", return_value=conn2):
        assert accounts.revoke_session("hash-2") is False
