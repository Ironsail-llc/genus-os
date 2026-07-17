"""Tests for robothor.engine.users.lookup_user.

Covers the identity-context fixes: the SELECT now returns person_id and the
stable ``tenant_users.user_id`` column (migration 037), the negative-cache
entry now expires (it used to live forever, making a newly-registered user
invisible until process restart), and the docstring no longer names the
operator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import users


@pytest.fixture(autouse=True)
def _clear_user_cache():
    users.clear_cache()
    yield
    users.clear_cache()


def _mock_conn(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_lookup_user_returns_stable_user_id_and_person_id():
    # Row order matches the new SELECT: tenant_id, display_name, role, user_id, person_id.
    row = ("t-alpha", "Alice", "owner", "usr-stable-abc", "person-9")
    conn, cur = _mock_conn(row)
    with patch("robothor.engine.users.get_connection", return_value=conn):
        result = users.lookup_user("999", tenant_id="t-alpha")

    assert result == {
        "tenant_id": "t-alpha",
        "display_name": "Alice",
        "role": "owner",
        "user_id": "usr-stable-abc",
        "person_id": "person-9",
    }


def test_lookup_user_select_no_longer_casts_serial_id():
    conn, cur = _mock_conn(None)
    with patch("robothor.engine.users.get_connection", return_value=conn):
        users.lookup_user("999", tenant_id="t-alpha")

    sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "id::TEXT as user_id" not in sql
    assert "user_id" in sql
    assert "person_id" in sql


def test_lookup_user_person_id_none_when_unlinked():
    row = ("t-alpha", "Bob", "member", "usr-2", None)
    conn, cur = _mock_conn(row)
    with patch("robothor.engine.users.get_connection", return_value=conn):
        result = users.lookup_user("111", tenant_id="t-alpha")

    assert result["person_id"] is None


def test_lookup_user_negative_result_is_cached_but_expires(monkeypatch):
    conn, cur = _mock_conn(None)
    fake_now = [0.0]
    monkeypatch.setattr(users.time, "monotonic", lambda: fake_now[0])
    with patch("robothor.engine.users.get_connection", return_value=conn) as mock_get_conn:
        assert users.lookup_user("404", tenant_id="t-alpha") is None
        assert users.lookup_user("404", tenant_id="t-alpha") is None
        assert mock_get_conn.call_count == 1  # served from cache, no restart-until-forever

        fake_now[0] += 61  # past the negative-cache TTL
        assert users.lookup_user("404", tenant_id="t-alpha") is None
        assert mock_get_conn.call_count == 2  # re-checked the DB — no longer invisible forever


def test_lookup_user_positive_result_is_cached_within_ttl(monkeypatch):
    row = ("t-alpha", "Alice", "owner", "usr-1", "person-1")
    conn, cur = _mock_conn(row)
    fake_now = [0.0]
    monkeypatch.setattr(users.time, "monotonic", lambda: fake_now[0])
    with patch("robothor.engine.users.get_connection", return_value=conn) as mock_get_conn:
        users.lookup_user("999", tenant_id="t-alpha")
        fake_now[0] += 30
        users.lookup_user("999", tenant_id="t-alpha")
    assert mock_get_conn.call_count == 1


def test_lookup_user_clear_cache_forces_refetch():
    row = ("t-alpha", "Alice", "owner", "usr-1", "person-1")
    conn, cur = _mock_conn(row)
    with patch("robothor.engine.users.get_connection", return_value=conn) as mock_get_conn:
        users.lookup_user("999", tenant_id="t-alpha")
        users.clear_cache()
        users.lookup_user("999", tenant_id="t-alpha")
    assert mock_get_conn.call_count == 2


def test_lookup_user_docstring_does_not_name_the_operator():
    doc = users.lookup_user.__doc__ or ""
    assert "Philip" not in doc
    assert "Delphi" not in doc
    assert "operator" in doc.lower()
