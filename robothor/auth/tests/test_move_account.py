"""``move_account``: refusal paths and dry run, with the database mocked.

The live move — every linked row moving in one transaction — is proved against
real Postgres in ``test_move_account_integration.py``. These tests pin the
decisions that must be taken BEFORE anything is written: each refusal leaves
the transaction with no write at all, and a dry run never commits.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.auth import accounts

ACCOUNT = {
    "id": "00000000-0000-0000-0000-00000000a11c",
    "tenant_id": "acme-parent",
    "email": "alice@example.com",
    "role": "member",
    "status": "active",
    "person_id": "00000000-0000-0000-0000-0000000000b0",
}
ROOT = {"id": "acme-parent", "active": True, "parent_tenant_id": None}
CHILD = {"id": "acme-child-a", "active": True, "parent_tenant_id": "acme-parent"}


class _Cursor:
    """Answers each statement by the first scripted needle its SQL contains."""

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []
        self._row: Any = None
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self._row = None
        self.rowcount = 1
        for needle, row in self.script:
            if needle in sql:
                self._row = row
                break

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return [self._row] if self._row else []

    def close(self) -> None:
        return None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def writes(self) -> list[str]:
        return [
            sql
            for sql, _ in self.executed
            if sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
        ]


def _conn(
    *,
    account: dict[str, Any] | None = ACCOUNT,
    target: dict[str, Any] | None = CHILD,
    other_owners: int = 1,
    email_taken: bool = False,
) -> tuple[MagicMock, _Cursor]:
    cur = _Cursor(
        [
            # Tenant ancestry (the root-shell guard): every tenant here is
            # under acme-parent. First, because it also reads crm_tenants.
            ("WITH RECURSIVE", {"id": "acme-parent"}),
            ("role = 'owner'", {"n": other_owners}),
            ("COUNT(*)", {"n": 1}),
            ("FROM user_accounts WHERE tenant_id", {"id": "x"} if email_taken else None),
            ("FROM user_accounts WHERE id", account),
            ("FROM crm_tenants", target),
            ("INSERT INTO audit_log", {"id": 42}),
        ]
    )
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    conn.autocommit = False
    return conn, cur


@pytest.fixture(autouse=True)
def _root_shell(monkeypatch) -> None:
    """These tests run as the root's shell; the guard has its own tests."""
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-parent")


def test_refuses_a_shell_bound_to_a_child(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-child-a")
    conn, cur = _conn()
    _refused(conn, cur, "root")


def test_refuses_a_dry_run_from_a_child_shell_too(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-child-a")
    conn, cur = _conn()
    _refused(conn, cur, "root", dry_run=True)


def _move(conn: MagicMock, **kwargs: Any) -> accounts.MoveReport:
    kwargs.setdefault("actor", "cli:test")
    to = kwargs.pop("to", "acme-child-a")
    with patch("robothor.auth.accounts.get_connection", return_value=conn):
        return accounts.move_account(ACCOUNT["id"], to, **kwargs)


def _refused(conn: MagicMock, cur: _Cursor, match: str, **kwargs: Any) -> None:
    with pytest.raises(accounts.AccountMoveError, match=match):
        _move(conn, **kwargs)
    assert cur.writes() == []
    conn.commit.assert_not_called()


# ─── refusals ────────────────────────────────────────────────────────────


def test_refuses_an_unknown_account() -> None:
    conn, cur = _conn(account=None)
    _refused(conn, cur, "no account")


def test_refuses_a_missing_target_tenant() -> None:
    conn, cur = _conn(target=None)
    _refused(conn, cur, "does not exist")


def test_refuses_an_inactive_target_tenant() -> None:
    conn, cur = _conn(target={**CHILD, "active": False})
    _refused(conn, cur, "not active")


def test_refuses_a_move_into_the_tenant_it_is_already_in() -> None:
    conn, cur = _conn(target=ROOT)
    _refused(conn, cur, "already", to="acme-parent")


def test_refuses_to_move_the_source_tenants_last_owner() -> None:
    conn, cur = _conn(account={**ACCOUNT, "role": "owner"}, target=ROOT, other_owners=0)
    _refused(conn, cur, "last owner", to="acme-other")


def test_refuses_an_owner_into_a_child_without_a_demotion() -> None:
    conn, cur = _conn(account={**ACCOUNT, "role": "owner"}, other_owners=1)
    _refused(conn, cur, "--role admin")


def test_refuses_when_the_target_already_has_that_email() -> None:
    conn, cur = _conn(email_taken=True)
    _refused(conn, cur, "already has an account")


def test_refuses_a_promotion_to_owner() -> None:
    conn, cur = _conn()
    _refused(conn, cur, "role", role="owner")


# ─── dry run and live ────────────────────────────────────────────────────


def test_dry_run_reports_counts_and_writes_nothing() -> None:
    conn, cur = _conn()
    report = _move(conn, dry_run=True)
    assert report.dry_run is True
    assert report.from_tenant == "acme-parent" and report.to_tenant == "acme-child-a"
    assert set(report.counts) == set(accounts.MOVED_TABLES)
    assert report.counts["user_accounts"] == 1
    assert cur.writes() == []
    conn.commit.assert_not_called()


def test_live_move_writes_audit_and_revokes_sessions_in_the_same_transaction() -> None:
    conn, cur = _conn()
    report = _move(conn, role="admin")
    writes = " ".join(cur.writes())
    assert "UPDATE user_accounts" in writes
    assert "UPDATE user_sessions" in writes
    assert "INSERT INTO audit_log" in writes
    conn.commit.assert_called_once()
    assert report.dry_run is False
    assert report.role_before == "member" and report.role_after == "admin"
    assert report.audit_id == 42


def test_report_serializes_for_json() -> None:
    conn, _ = _conn()
    report = _move(conn, dry_run=True)
    data = report.to_dict()
    assert data["to_tenant"] == "acme-child-a"
    assert data["counts"]["user_accounts"] == 1


# ─── the calling process's session-standing cache ────────────────────────


def _standing_module(monkeypatch) -> MagicMock:
    """A stand-in ``robothor.auth.session_standing`` (added by the session
    standing work). When it is there, the move drops the entry in the
    process that ran the move; other processes' caches expire on their own."""
    import sys
    import types

    module = types.ModuleType("robothor.auth.session_standing")
    module.forget = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "robothor.auth.session_standing", module)
    return module.forget  # type: ignore[attr-defined,no-any-return]


def test_a_live_move_forgets_the_accounts_standing_in_this_process(monkeypatch) -> None:
    forget = _standing_module(monkeypatch)
    conn, _ = _conn()
    _move(conn)
    forget.assert_called_once_with(ACCOUNT["id"])


def test_a_dry_run_forgets_nothing(monkeypatch) -> None:
    forget = _standing_module(monkeypatch)
    conn, _ = _conn()
    _move(conn, dry_run=True)
    forget.assert_not_called()


def test_a_move_works_where_the_standing_cache_does_not_exist(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "robothor.auth.session_standing", None)
    conn, _ = _conn()
    assert _move(conn).dry_run is False
