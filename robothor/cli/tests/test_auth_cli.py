"""Tests for ``robothor auth`` CLI commands."""

from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import patch

from robothor.cli.auth import cmd_auth

_ACCOUNT = {"email": "owner@example.com", "tenant_id": "default", "role": "owner"}


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
