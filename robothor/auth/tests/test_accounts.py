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
    # account, especially an administrator account.
    existing = {
        "id": "uid-2",
        "email": "owner@example.com",
        "role": "owner",
        "status": "active",
    }
    conn, cur = _mock_conn([None, existing])
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
