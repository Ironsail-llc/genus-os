"""accounts DAL: JIT provisioning paths + session revoke (DB mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    new_row = {"id": "uid-1", "email": "a@x.com", "role": "member", "tenant_id": "default"}
    conn, cur = _mock_conn([None, None, new_row])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://idp", subject="sub-1", email="a@x.com", display_name="Ann"
        )
    assert out["id"] == "uid-1"
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "INSERT INTO user_accounts" in sql
    conn.commit.assert_called()


def test_jit_provision_links_existing_email_account():
    # idp lookup -> None; email lookup -> existing row; UPDATE RETURNING -> linked row.
    existing = {"id": "uid-2", "email": "b@x.com", "role": "admin"}
    linked = {**existing, "idp_issuer": "https://idp"}
    conn, cur = _mock_conn([None, existing, linked])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://idp", subject="sub-2", email="b@x.com", display_name="Bob"
        )
    assert out["id"] == "uid-2"
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "UPDATE user_accounts" in sql and "idp_issuer" in sql


def test_jit_provision_returns_existing_idp_account():
    # idp lookup -> existing row; _touch_login UPDATE RETURNING -> same row.
    existing = {"id": "uid-3", "email": "c@x.com", "role": "owner"}
    conn, cur = _mock_conn([existing, existing])
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        out = accounts.jit_provision(
            issuer="https://idp", subject="sub-3", email="c@x.com", display_name="Cy"
        )
    assert out["id"] == "uid-3"
    sql = " ".join(str(c[0][0]) for c in cur.execute.call_args_list)
    assert "INSERT INTO user_accounts" not in sql  # not re-created
    assert "last_login_at = NOW()" in sql


def test_revoke_session_true_then_false():
    conn, cur = _mock_conn([], rowcount=1)
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        assert accounts.revoke_session("hash-1") is True
    conn2, cur2 = _mock_conn([], rowcount=0)
    with patch("robothor.auth.accounts.get_connection", return_value=conn2):
        assert accounts.revoke_session("hash-2") is False
