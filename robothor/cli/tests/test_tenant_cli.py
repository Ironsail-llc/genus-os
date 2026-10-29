"""``genus tenant set-domains``, ``status`` domains, and ``move-account`` (DAL mocked)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from argparse import Namespace

from robothor.auth import accounts
from robothor.cli import _build_parser
from robothor.cli.tenant import cmd_tenant
from robothor.crm.tenant_domains import InvalidSsoDomainError, SsoDomainConflictError

ALICE_PARENT = {
    "id": "00000000-0000-0000-0000-00000000a11c",
    "tenant_id": "acme-parent",
    "email": "alice@example.com",
    "role": "member",
    "status": "active",
}
ALICE_OTHER = {
    **ALICE_PARENT,
    "id": "00000000-0000-0000-0000-00000000a11d",
    "tenant_id": "acme-other",
}


def _report(**overrides: Any) -> accounts.MoveReport:
    fields: dict[str, Any] = {
        "user_id": ALICE_PARENT["id"],
        "email": "alice@example.com",
        "from_tenant": "acme-parent",
        "to_tenant": "acme-child-a",
        "role_before": "member",
        "role_after": "member",
        "dry_run": False,
        "counts": dict.fromkeys(accounts.MOVED_TABLES, 1),
        "sessions_revoked": 2,
        "audit_id": 7,
    }
    fields.update(overrides)
    return accounts.MoveReport(**fields)


def _parse(*argv: str) -> Namespace:
    return _build_parser().parse_args(["tenant", *argv])


# ─── parser surface ──────────────────────────────────────────────────────


def test_parser_accepts_move_account() -> None:
    args = _parse(
        "move-account", "--email", "alice@example.com", "--to", "acme-child-a",
        "--from", "acme-parent", "--role", "admin", "--dry-run", "--json",
    )  # fmt: skip
    assert args.tenant_command == "move-account"
    assert args.email == "alice@example.com"
    assert args.to == "acme-child-a"
    assert args.from_tenant == "acme-parent"
    assert args.role == "admin"
    assert args.dry_run is True and args.json is True


def test_parser_refuses_owner_as_a_move_role() -> None:
    with pytest.raises(SystemExit):
        _parse("move-account", "--email", "alice@example.com", "--to", "x", "--role", "owner")


def test_parser_requires_email_and_target() -> None:
    with pytest.raises(SystemExit):
        _parse("move-account", "--email", "alice@example.com")
    with pytest.raises(SystemExit):
        _parse("move-account", "--to", "acme-child-a")


def test_parser_accepts_set_domains() -> None:
    args = _parse("set-domains", "acme-parent", "example.com", "other.example")
    assert args.tenant_command == "set-domains"
    assert args.tenant_id == "acme-parent"
    assert args.domains == ["example.com", "other.example"]


# ─── set-domains / status ────────────────────────────────────────────────


def test_set_domains_merges_into_existing_settings(capsys) -> None:
    tenant = {"id": "acme-parent", "settings": {"theme": "dark"}}
    with (
        patch("robothor.crm.dal.get_tenant", return_value=tenant),
        patch("robothor.crm.dal.update_tenant", return_value=True) as upd,
    ):
        rc = cmd_tenant(_parse("set-domains", "acme-parent", "Example.com", "other.example"))
    assert rc == 0
    assert upd.call_args.args[0] == "acme-parent"
    assert upd.call_args.kwargs["settings"] == {
        "theme": "dark",
        "sso_domains": ["example.com", "other.example"],
    }
    assert capsys.readouterr().out == (
        "Tenant 'acme-parent' SSO domains: example.com, other.example\n"
    )


def test_set_domains_with_no_domains_clears_the_claim() -> None:
    tenant = {"id": "acme-parent", "settings": {"sso_domains": ["example.com"]}}
    with (
        patch("robothor.crm.dal.get_tenant", return_value=tenant),
        patch("robothor.crm.dal.update_tenant", return_value=True) as upd,
    ):
        assert cmd_tenant(_parse("set-domains", "acme-parent")) == 0
    assert upd.call_args.kwargs["settings"] == {"sso_domains": []}


def test_set_domains_reports_a_conflict(capsys) -> None:
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme-child-a", "settings": {}}),
        patch(
            "robothor.crm.dal.update_tenant",
            side_effect=SsoDomainConflictError("example.com", "acme-parent"),
        ),
    ):
        rc = cmd_tenant(_parse("set-domains", "acme-child-a", "example.com"))
    assert rc == 1
    assert capsys.readouterr().err == (
        "Error: domain 'example.com' is already claimed by tenant 'acme-parent'\n"
    )


def test_set_domains_reports_a_malformed_domain(capsys) -> None:
    with (
        patch("robothor.crm.dal.get_tenant", return_value={"id": "acme-parent", "settings": {}}),
        patch("robothor.crm.dal.update_tenant", side_effect=InvalidSsoDomainError("bad")),
    ):
        assert cmd_tenant(_parse("set-domains", "acme-parent", "not_a_domain")) == 1


def test_set_domains_on_a_missing_tenant_fails() -> None:
    with (
        patch("robothor.crm.dal.get_tenant", return_value=None),
        patch("robothor.crm.dal.update_tenant") as upd,
    ):
        assert cmd_tenant(_parse("set-domains", "nope", "example.com")) == 1
    upd.assert_not_called()


def test_status_shows_sso_domains(capsys) -> None:
    tenant = {
        "id": "acme-parent",
        "displayName": "Acme Parent",
        "settings": {"sso_domains": ["example.com", "other.example"]},
        "active": True,
        "parentTenantId": None,
    }
    with (
        patch("robothor.crm.dal.get_tenant", return_value=tenant),
        patch("robothor.memory.blocks.list_blocks", side_effect=RuntimeError("no db")),
        patch("robothor.db.get_connection", side_effect=RuntimeError("no db")),
    ):
        assert cmd_tenant(_parse("status", "acme-parent")) == 0
    out = capsys.readouterr().out
    assert "  SSO domains: example.com, other.example" in out.splitlines()
    # get_tenant returns the API shape (camelCase); status must read it.
    assert "Acme Parent" in out


def test_status_reads_blocks_and_runs_in_the_tenant_asked_about(capsys) -> None:
    """From a root shell with RLS on, a read in the shell's own scope sees none
    of a child's rows: status reported 0 blocks and 0 runs for a busy child."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from robothor.db.connection import current_tenant_scope

    seen: dict[str, object] = {}

    def blocks(tenant_id: str) -> dict:
        seen["blocks"] = current_tenant_scope()
        return {"blocks": [{"name": "persona", "size": 10}]}

    @contextmanager
    def connection():
        seen["runs"] = current_tenant_scope()
        cur = MagicMock()
        cur.fetchone.return_value = (16,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        yield conn

    tenant = {"id": "acme-child-a", "displayName": "A", "settings": {}, "active": True}
    with (
        patch("robothor.crm.dal.get_tenant", return_value=tenant),
        patch("robothor.memory.blocks.list_blocks", side_effect=blocks),
        patch("robothor.db.get_connection", side_effect=connection),
    ):
        assert cmd_tenant(_parse("status", "acme-child-a")) == 0
    assert seen == {"blocks": "acme-child-a", "runs": "acme-child-a"}
    assert "Runs (last 7d): 16" in capsys.readouterr().out


# ─── move-account ────────────────────────────────────────────────────────


def test_move_account_moves_the_single_match(capsys) -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account", return_value=_report()) as mv,
    ):
        rc = cmd_tenant(
            _parse("move-account", "--email", "alice@example.com", "--to", "acme-child-a")
        )
    assert rc == 0
    assert mv.call_args.args == (ALICE_PARENT["id"], "acme-child-a")
    assert mv.call_args.kwargs["role"] is None
    assert mv.call_args.kwargs["dry_run"] is False
    assert mv.call_args.kwargs["actor"].startswith("cli:")
    out = capsys.readouterr().out
    assert "acme-parent" in out and "acme-child-a" in out


def test_move_account_passes_role_and_dry_run() -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account", return_value=_report(dry_run=True)) as mv,
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a", "--role", "admin")
        rc = cmd_tenant(_parse("move-account", *argv, "--dry-run"))
    assert rc == 0
    assert mv.call_args.kwargs["role"] == "admin"
    assert mv.call_args.kwargs["dry_run"] is True


def test_move_account_passes_detach_person() -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account", return_value=_report()) as mv,
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a", "--detach-person")
        assert cmd_tenant(_parse("move-account", *argv)) == 0
    assert mv.call_args.kwargs["detach_person"] is True


def test_move_account_does_not_detach_by_default() -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account", return_value=_report()) as mv,
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a")
        assert cmd_tenant(_parse("move-account", *argv)) == 0
    assert mv.call_args.kwargs["detach_person"] is False


def test_move_account_json_output(capsys) -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account", return_value=_report()),
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a", "--json")
        assert cmd_tenant(_parse("move-account", *argv)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["to_tenant"] == "acme-child-a"
    assert data["counts"]["user_accounts"] == 1


def test_move_account_ambiguous_email_lists_tenants_and_moves_nothing(capsys) -> None:
    with (
        patch(
            "robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT, ALICE_OTHER]
        ),
        patch("robothor.auth.accounts.move_account") as mv,
    ):
        rc = cmd_tenant(
            _parse("move-account", "--email", "alice@example.com", "--to", "acme-child-a")
        )
    assert rc == 1
    mv.assert_not_called()
    err = capsys.readouterr().err
    assert "acme-parent" in err and "acme-other" in err and "--from" in err


def test_move_account_from_resolves_an_ambiguous_email() -> None:
    with (
        patch(
            "robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT, ALICE_OTHER]
        ),
        patch("robothor.auth.accounts.move_account", return_value=_report()) as mv,
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a", "--from", "acme-other")
        assert cmd_tenant(_parse("move-account", *argv)) == 0
    assert mv.call_args.args[0] == ALICE_OTHER["id"]


def test_move_account_unknown_email_fails(capsys) -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[]),
        patch("robothor.auth.accounts.move_account") as mv,
    ):
        rc = cmd_tenant(
            _parse("move-account", "--email", "bob@example.com", "--to", "acme-child-a")
        )
    assert rc == 1
    mv.assert_not_called()
    assert capsys.readouterr().err == "Error: no account for bob@example.com.\n"


def test_move_account_from_that_matches_nothing_fails() -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch("robothor.auth.accounts.move_account") as mv,
    ):
        argv = ("--email", "alice@example.com", "--to", "acme-child-a", "--from", "acme-other")
        assert cmd_tenant(_parse("move-account", *argv)) == 1
    mv.assert_not_called()


def test_move_account_reports_a_refusal(capsys) -> None:
    with (
        patch("robothor.auth.accounts.accounts_for_email", return_value=[ALICE_PARENT]),
        patch(
            "robothor.auth.accounts.move_account",
            side_effect=accounts.AccountMoveError("target tenant 'x' does not exist"),
        ),
    ):
        rc = cmd_tenant(_parse("move-account", "--email", "alice@example.com", "--to", "x"))
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err
