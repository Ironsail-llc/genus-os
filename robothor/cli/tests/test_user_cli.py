"""Tests for ``robothor user`` CLI commands (closed-allowlist registration)."""

from __future__ import annotations

from argparse import Namespace
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

from robothor.cli.user import VALID_ROLES, cmd_user


def _unique_violation(constraint_name: str) -> psycopg2.errors.UniqueViolation:
    """Build a ``UniqueViolation`` whose ``.diag.constraint_name`` is set.

    ``psycopg2.Error.diag`` is a read-only C-level property normally
    populated by the driver from the server's error response, so a plain
    instance's ``.diag`` cannot be assigned directly. A subclass property
    shadows it fine (attribute lookup walks the MRO from the most-derived
    class), which is all the mocked constraint-name dispatch needs.
    """

    class _Fake(psycopg2.errors.UniqueViolation):
        @property
        def diag(self) -> Any:
            return MagicMock(constraint_name=constraint_name)

    return _Fake("duplicate key")


def _mock_conn(cursor: MagicMock) -> MagicMock:
    """Build a ``get_connection()`` context-manager mock around ``cursor``."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn


# ─── dispatch ────────────────────────────────────────────────────────────


def test_cmd_user_no_subcommand_returns_1(capsys) -> None:
    rc = cmd_user(Namespace(user_command=None))
    assert rc == 1
    assert "usage" in capsys.readouterr().out.lower()


# ─── list ────────────────────────────────────────────────────────────────


def test_user_list_prints_table(capsys) -> None:
    cur = MagicMock()
    cur.fetchall.return_value = [
        ("111", "Alice Example", "member", "acme", True, "person-1", "alice@example.com"),
    ]
    args = Namespace(user_command="list", tenant=None)
    with patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)):
        rc = cmd_user(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "111" in out
    assert "Alice Example" in out
    assert "member" in out
    assert "acme" in out
    assert "alice@example.com" in out
    assert "y" in out  # linked=y


def test_user_list_empty(capsys) -> None:
    cur = MagicMock()
    cur.fetchall.return_value = []
    args = Namespace(user_command="list", tenant=None)
    with patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)):
        rc = cmd_user(args)

    assert rc == 0
    assert "No tenant users" in capsys.readouterr().out


def test_user_list_filters_by_tenant() -> None:
    cur = MagicMock()
    cur.fetchall.return_value = []
    args = Namespace(user_command="list", tenant="acme")
    with patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)):
        cmd_user(args)

    sql, params = cur.execute.call_args[0]
    assert "WHERE" in sql
    assert params == ("acme",)


# ─── add: argument validation ───────────────────────────────────────────


def test_user_add_rejects_invalid_role(capsys) -> None:
    args = Namespace(
        user_command="add",
        tenant="acme",
        name="Alice Example",
        role="superuser",
        telegram_id=None,
        email=None,
        person_id=None,
        create_person=False,
    )
    rc = cmd_user(args)
    assert rc == 2
    assert "role" in capsys.readouterr().err.lower()


def test_user_add_rejects_person_id_and_create_person_together(capsys) -> None:
    args = Namespace(
        user_command="add",
        tenant="acme",
        name="Alice Example",
        role="member",
        telegram_id=None,
        email=None,
        person_id="11111111-1111-1111-1111-111111111111",
        create_person=True,
    )
    rc = cmd_user(args)
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err.lower()


def test_valid_roles_matches_platform_human_roles() -> None:
    # Single source of truth: robothor.auth.tokens._HUMAN_ROLES. If that set
    # changes, this constant must be updated too (see module docstring).
    from robothor.auth.tokens import _HUMAN_ROLES

    assert VALID_ROLES == _HUMAN_ROLES


# ─── add: tenant / person resolution ────────────────────────────────────


def _base_add_args(**overrides: object) -> Namespace:
    base: dict[str, object] = {
        "user_command": "add",
        "tenant": "acme",
        "name": "Alice Example",
        "role": "member",
        "telegram_id": "555",
        "email": "alice@example.com",
        "person_id": None,
        "create_person": False,
    }
    base.update(overrides)
    return Namespace(**base)


def test_user_add_unknown_tenant_returns_1(capsys) -> None:
    args = _base_add_args()
    with patch("robothor.crm.dal.get_tenant", return_value=None):
        rc = cmd_user(args)
    assert rc == 1
    assert "tenant" in capsys.readouterr().err.lower()


def test_user_add_person_id_not_found_returns_1(capsys) -> None:
    args = _base_add_args(person_id="11111111-1111-1111-1111-111111111111")
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.get_person", return_value=None),
    ):
        rc = cmd_user(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


# ─── add: full linkage (happy path) ─────────────────────────────────────


def test_user_add_creates_full_linkage(capsys) -> None:
    args = _base_add_args()
    cur = MagicMock()
    # _find_person_by_email lookup (no match), tenant_users insert, telegram
    # contact_identifiers, email contact_identifiers, user_accounts invite --
    # each RETURNING a row.
    cur.fetchone.side_effect = [
        None,  # _find_person_by_email: no existing person
        (42, "stable-user-id"),  # tenant_users insert
        (1,),  # telegram contact_identifiers insert
        (2,),  # email contact_identifiers insert
        ("acct-1",),  # user_accounts insert
    ]
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.create_person", return_value="person-99") as create_person,
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
        patch("robothor.memory.entities.upsert_entity", return_value=7),
    ):
        rc = cmd_user(args)

    assert rc == 0
    create_person.assert_called_once()
    out = capsys.readouterr().out
    assert "person-99" in out
    assert "tenant_users" in out
    assert "telegram:555" in out
    assert "email:alice@example.com" in out
    assert "invite" in out.lower()
    # The follow-up hint must be copy-pasteable and correct.
    assert "robothor auth grant-binding --email alice@example.com --tenant acme" in out


def test_user_add_reuses_existing_person_by_email(capsys) -> None:
    args = _base_add_args()
    cur = MagicMock()
    cur.fetchone.side_effect = [
        ("person-1",),  # _find_person_by_email lookup
        (42, "stable-user-id"),  # tenant_users insert
        (1,),  # telegram ci
        (2,),  # email ci
        ("acct-1",),  # user_accounts insert
    ]
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.create_person") as create_person,
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
        patch("robothor.memory.entities.upsert_entity", return_value=7),
    ):
        rc = cmd_user(args)

    assert rc == 0
    create_person.assert_not_called()
    assert "person-1" in capsys.readouterr().out


def test_user_add_memory_entity_failure_degrades_gracefully(capsys) -> None:
    args = _base_add_args()
    cur = MagicMock()
    cur.fetchone.side_effect = [
        None,  # _find_person_by_email: no existing person
        (42, "stable-user-id"),
        (1,),
        (2,),
        ("acct-1",),
    ]
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.create_person", return_value="person-99"),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
        patch("robothor.memory.entities.upsert_entity", side_effect=RuntimeError("db down")),
    ):
        rc = cmd_user(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "memory entity" in out.lower()
    assert "notice" in out.lower() or "skipped" in out.lower()


# ─── add: owner uniqueness ───────────────────────────────────────────────


def test_user_add_owner_uniqueness_violation_is_clear(capsys) -> None:
    args = _base_add_args(role="owner")
    cur = MagicMock()
    cur.fetchone.return_value = None  # _find_person_by_email: no existing person
    exc = _unique_violation("uq_tenant_users_owner_person")
    cur.execute.side_effect = [None, exc]  # find-by-email SELECT, then the failing INSERT
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.create_person", return_value="person-99"),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "owner" in err
    assert "already" in err


def test_user_add_telegram_already_registered_is_clear(capsys) -> None:
    args = _base_add_args()
    cur = MagicMock()
    cur.fetchone.return_value = None  # _find_person_by_email: no existing person
    exc = _unique_violation("tenant_users_telegram_tenant_key")
    cur.execute.side_effect = [None, exc]  # find-by-email SELECT, then the failing INSERT
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme"}),
        patch("robothor.crm.dal.create_person", return_value="person-99"),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "555" in err
    assert "already registered" in err


# ─── link ─────────────────────────────────────────────────────────────────


def test_user_link_updates_person_and_contact_identifier(capsys) -> None:
    args = Namespace(
        user_command="link",
        telegram_id="555",
        tenant="acme",
        person_id="person-99",
        email=None,
    )
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (42, "Alice Example"),  # tenant_users UPDATE ... RETURNING id, display_name
        (7,),  # contact_identifiers upsert RETURNING id
    ]
    with (
        patch("robothor.crm.dal.get_person", return_value={"id": "person-99"}),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 0
    assert "person-99" in capsys.readouterr().out


def test_user_link_unregistered_telegram_id_returns_1(capsys) -> None:
    args = Namespace(
        user_command="link",
        telegram_id="555",
        tenant="acme",
        person_id="person-99",
        email=None,
    )
    cur = MagicMock()
    cur.fetchone.return_value = None  # UPDATE ... RETURNING matched nothing
    with (
        patch("robothor.crm.dal.get_person", return_value={"id": "person-99"}),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    assert "no tenant_users row" in capsys.readouterr().err.lower()


def test_user_link_owner_uniqueness_violation_is_clear(capsys) -> None:
    args = Namespace(
        user_command="link",
        telegram_id="555",
        tenant="acme",
        person_id="person-99",
        email=None,
    )
    cur = MagicMock()
    cur.execute.side_effect = _unique_violation("uq_tenant_users_owner_person")
    with (
        patch("robothor.crm.dal.get_person", return_value={"id": "person-99"}),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "owner" in err
    assert "already" in err


def test_user_link_email_not_found_returns_1(capsys) -> None:
    args = Namespace(
        user_command="link",
        telegram_id="555",
        tenant="acme",
        person_id=None,
        email="ghost@example.com",
    )
    cur = MagicMock()
    with (
        patch("robothor.cli.user._find_person_by_email", return_value=None),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    assert "ghost@example.com" in capsys.readouterr().err


# ─── link-face ──────────────────────────────────────────────────────────


def test_user_link_face_success(capsys) -> None:
    args = Namespace(
        user_command="link-face",
        label="alice-front",
        person_id="person-99",
        tenant="acme",
    )
    cur = MagicMock()
    cur.fetchone.side_effect = [("public.face_identities",), ("person-99",)]
    with (
        patch("robothor.crm.dal.get_person", return_value={"id": "person-99"}),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 0
    assert "alice-front" in capsys.readouterr().out


def test_user_link_face_table_missing_degrades_gracefully(capsys) -> None:
    args = Namespace(
        user_command="link-face",
        label="alice-front",
        person_id="person-99",
        tenant="acme",
    )
    cur = MagicMock()
    cur.fetchone.return_value = None  # to_regclass found nothing
    with (
        patch("robothor.crm.dal.get_person", return_value={"id": "person-99"}),
        patch("robothor.db.connection.get_connection", return_value=_mock_conn(cur)),
    ):
        rc = cmd_user(args)

    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "face_identities not present" in err


def test_user_link_face_person_not_found_returns_1(capsys) -> None:
    args = Namespace(
        user_command="link-face",
        label="alice-front",
        person_id="person-99",
        tenant="acme",
    )
    with patch("robothor.crm.dal.get_person", return_value=None):
        rc = cmd_user(args)

    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("role", sorted(VALID_ROLES))
def test_all_valid_roles_accepted_by_guard(role: str) -> None:
    from robothor.cli.user import _validate_role

    assert _validate_role(role) is True
