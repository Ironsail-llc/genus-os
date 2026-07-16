"""Tests for ``robothor auth`` CLI commands."""

from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import patch

import pytest

from robothor.cli.auth import _parse_ttl, cmd_auth

_ACCOUNT = {"email": "owner@example.com", "tenant_id": "default", "role": "owner"}

_GRANT = {
    "id": "grant-1",
    "email": "owner@example.com",
    "tenant_id": "default",
    "expires_at": "2026-01-01T00:30:00+00:00",
    "used_at": None,
    "revoked_at": None,
    "reason": "initial owner binding",
}


def test_auth_bootstrap_seeds_owner(capsys) -> None:
    args = Namespace(auth_command="bootstrap", json_output=False)
    with patch("robothor.auth.accounts.bootstrap_owner_account", return_value=_ACCOUNT):
        rc = cmd_auth(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "owner@example.com" in out
    assert "owner" in out


def test_auth_bootstrap_json_output(capsys) -> None:
    args = Namespace(auth_command="bootstrap", json_output=True)
    with patch("robothor.auth.accounts.bootstrap_owner_account", return_value=_ACCOUNT):
        rc = cmd_auth(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["email"] == "owner@example.com"
    assert payload["role"] == "owner"


def test_auth_bootstrap_no_operator_returns_nonzero(capsys) -> None:
    args = Namespace(auth_command="bootstrap", json_output=False)
    with patch("robothor.auth.accounts.bootstrap_owner_account", return_value=None):
        rc = cmd_auth(args)

    assert rc == 1
    assert "No operator" in capsys.readouterr().out


def test_auth_no_subcommand_returns_nonzero(capsys) -> None:
    rc = cmd_auth(Namespace(auth_command=None))
    assert rc == 1


def test_auth_grant_binding_arms_grant(capsys) -> None:
    args = Namespace(
        auth_command="grant-binding",
        email="Owner@Example.com",
        tenant=None,
        ttl="30m",
        reason="initial owner binding",
        json_output=False,
    )
    with patch("robothor.auth.accounts.create_binding_grant", return_value=_GRANT) as create:
        rc = cmd_auth(args)

    assert rc == 0
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["email"] == "Owner@Example.com"
    assert kwargs["ttl_seconds"] == 1800
    assert kwargs["reason"] == "initial owner binding"
    out = capsys.readouterr().out
    assert "grant-1" in out
    assert "owner@example.com" in out


def test_auth_grant_binding_json_output(capsys) -> None:
    args = Namespace(
        auth_command="grant-binding",
        email="owner@example.com",
        tenant="default",
        ttl="15m",
        reason="",
        json_output=True,
    )
    with patch("robothor.auth.accounts.create_binding_grant", return_value=_GRANT):
        rc = cmd_auth(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "grant-1"


def test_auth_grant_binding_rejects_bad_ttl(capsys) -> None:
    args = Namespace(
        auth_command="grant-binding",
        email="owner@example.com",
        tenant=None,
        ttl="garbage",
        reason="",
        json_output=False,
    )
    with patch("robothor.auth.accounts.create_binding_grant") as create:
        rc = cmd_auth(args)

    assert rc == 2
    create.assert_not_called()
    assert "ttl" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("45s", 45), ("15m", 900), ("2h", 7200), ("1d", 86400), ("90", 90)],
)
def test_parse_ttl_accepts_suffixes(value: str, seconds: int) -> None:
    assert _parse_ttl(value) == seconds


@pytest.mark.parametrize("value", ["", "0", "-5m", "garbage", "5w", "1.5h"])
def test_parse_ttl_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        _parse_ttl(value)


def test_auth_grants_lists_active(capsys) -> None:
    args = Namespace(auth_command="grants", tenant=None, include_inactive=False, json_output=False)
    with patch("robothor.auth.accounts.list_binding_grants", return_value=[_GRANT]) as lister:
        rc = cmd_auth(args)

    assert rc == 0
    assert lister.call_args.kwargs["include_inactive"] is False
    out = capsys.readouterr().out
    assert "grant-1" in out
    assert "owner@example.com" in out


def test_auth_grants_all_includes_inactive(capsys) -> None:
    args = Namespace(auth_command="grants", tenant=None, include_inactive=True, json_output=True)
    used = {**_GRANT, "used_at": "2026-01-01T00:10:00+00:00"}
    with patch("robothor.auth.accounts.list_binding_grants", return_value=[used]) as lister:
        rc = cmd_auth(args)

    assert rc == 0
    assert lister.call_args.kwargs["include_inactive"] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["used_at"] is not None


def test_auth_revoke_binding(capsys) -> None:
    grant_id = "3e2a1a37-9d5f-4a2b-8f5e-1c2d3e4f5a6b"
    args = Namespace(auth_command="revoke-binding", grant_id=grant_id)
    with patch("robothor.auth.accounts.revoke_binding_grant", return_value=True):
        assert cmd_auth(args) == 0
    with patch("robothor.auth.accounts.revoke_binding_grant", return_value=False):
        assert cmd_auth(args) == 1


def test_auth_revoke_binding_rejects_non_uuid(capsys) -> None:
    # Validate UUIDs at the boundary — garbage must not reach the database.
    args = Namespace(auth_command="revoke-binding", grant_id="not-a-uuid")
    with patch("robothor.auth.accounts.revoke_binding_grant") as revoke:
        assert cmd_auth(args) == 2
    revoke.assert_not_called()
    assert "uuid" in capsys.readouterr().err.lower()


def test_auth_grant_binding_passes_issuer_pin(capsys) -> None:
    args = Namespace(
        auth_command="grant-binding",
        email="owner@example.com",
        tenant=None,
        ttl="15m",
        reason="",
        issuer="https://team.example.com",
        json_output=False,
    )
    with patch("robothor.auth.accounts.create_binding_grant", return_value=_GRANT) as create:
        assert cmd_auth(args) == 0
    assert create.call_args.kwargs["issuer"] == "https://team.example.com"


def test_auth_grant_binding_surfaces_unusable_target(capsys) -> None:
    from robothor.auth.accounts import GrantTargetError

    args = Namespace(
        auth_command="grant-binding",
        email="typo@example.com",
        tenant=None,
        ttl="15m",
        reason="",
        json_output=False,
    )
    with patch(
        "robothor.auth.accounts.create_binding_grant",
        side_effect=GrantTargetError("no account with email 'typo@example.com'"),
    ):
        assert cmd_auth(args) == 1
    assert "no account" in capsys.readouterr().err


def test_auth_grants_shows_expired_state(capsys) -> None:
    args = Namespace(auth_command="grants", tenant=None, include_inactive=True, json_output=False)
    expired = {**_GRANT, "state": "expired"}
    with patch("robothor.auth.accounts.list_binding_grants", return_value=[expired]):
        assert cmd_auth(args) == 0
    assert "expired" in capsys.readouterr().out
