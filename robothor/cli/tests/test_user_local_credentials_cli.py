"""``genus user set-password`` / ``genus user mfa-reset``.

The operator's recovery path when local login is the only way in. Both are
tested for what they must NOT do as much as what they must: never echo a
password, never print a TOTP secret, never write a plaintext credential.
"""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

from robothor.cli.user import cmd_user

ACCOUNT = {
    "id": "uid-1",
    "tenant_id": "default",
    "email": "alice@example.com",
    "display_name": "Alice",
    "role": "owner",
    "status": "active",
    "password_hash": None,
    "mfa_enabled": True,
    "mfa_secret_enc": "sealed",
}

SECRET = "a-long-enough-passphrase"


def _args(**kw) -> Namespace:
    base = {"user_command": None, "tenant": None, "email": "alice@example.com"}
    base.update(kw)
    return Namespace(**base)


# ── set-password ─────────────────────────────────────────────────────


def test_set_password_hashes_with_argon2id_and_never_prints_it(capsys) -> None:
    stored: list[tuple[str, str]] = []
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=dict(ACCOUNT)),
        patch(
            "robothor.auth.local_login._store_password_hash",
            side_effect=lambda uid, h: stored.append((uid, h)),
        ),
        patch("robothor.auth.accounts.revoke_user_sessions"),
        patch("robothor.cli.user.getpass", side_effect=[SECRET, SECRET]),
    ):
        rc = cmd_user(_args(user_command="set-password", password_stdin=False))

    assert rc == 0
    assert stored and stored[0][0] == "uid-1"
    assert stored[0][1].startswith("$argon2id$")
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err
    assert stored[0][1] not in out.out


def test_set_password_refuses_a_mismatched_confirmation(capsys) -> None:
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=dict(ACCOUNT)),
        patch("robothor.auth.local_login._store_password_hash") as store,
        patch("robothor.cli.user.getpass", side_effect=[SECRET, "something-else-entirely"]),
    ):
        rc = cmd_user(_args(user_command="set-password", password_stdin=False))
    assert rc == 1
    store.assert_not_called()
    assert "match" in capsys.readouterr().err.lower()


def test_set_password_enforces_the_minimum_length(capsys) -> None:
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=dict(ACCOUNT)),
        patch("robothor.auth.local_login._store_password_hash") as store,
        patch("robothor.cli.user.getpass", side_effect=["short", "short"]),
    ):
        rc = cmd_user(_args(user_command="set-password", password_stdin=False))
    assert rc == 1
    store.assert_not_called()
    assert "12" in capsys.readouterr().err


def test_set_password_reads_stdin_for_automation(capsys, monkeypatch) -> None:
    stored: list[tuple[str, str]] = []
    stdin = MagicMock()
    stdin.read.return_value = f"{SECRET}\n"
    monkeypatch.setattr("sys.stdin", stdin)
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=dict(ACCOUNT)),
        patch(
            "robothor.auth.local_login._store_password_hash",
            side_effect=lambda uid, h: stored.append((uid, h)),
        ),
        patch("robothor.auth.accounts.revoke_user_sessions"),
        patch("robothor.cli.user.getpass") as prompt,
    ):
        rc = cmd_user(_args(user_command="set-password", password_stdin=True))
    assert rc == 0
    prompt.assert_not_called()
    assert stored[0][1].startswith("$argon2id$")
    assert SECRET not in capsys.readouterr().out


def test_set_password_reports_an_unknown_email_without_writing(capsys) -> None:
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=None),
        patch("robothor.auth.local_login._store_password_hash") as store,
        patch("robothor.cli.user.getpass", side_effect=[SECRET, SECRET]),
    ):
        rc = cmd_user(_args(user_command="set-password", password_stdin=False))
    assert rc == 1
    store.assert_not_called()


# ── mfa-reset ────────────────────────────────────────────────────────


def test_mfa_reset_clears_the_factor(capsys) -> None:
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=dict(ACCOUNT)),
        patch("robothor.auth.local_login._clear_mfa") as clear,
    ):
        rc = cmd_user(_args(user_command="mfa-reset"))
    assert rc == 0
    clear.assert_called_once_with("uid-1")
    out = capsys.readouterr().out
    assert "sealed" not in out
    assert "secret" not in out.lower()


def test_mfa_reset_reports_an_unknown_email(capsys) -> None:
    with (
        patch("robothor.auth.accounts.get_account_by_email", return_value=None),
        patch("robothor.auth.local_login._clear_mfa") as clear,
    ):
        rc = cmd_user(_args(user_command="mfa-reset"))
    assert rc == 1
    clear.assert_not_called()


# ── argparse wiring ──────────────────────────────────────────────────


def test_both_commands_are_registered_on_the_cli() -> None:
    from robothor.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["user", "set-password", "alice@example.com"])
    assert args.user_command == "set-password" and args.email == "alice@example.com"
    args = parser.parse_args(["user", "mfa-reset", "alice@example.com"])
    assert args.user_command == "mfa-reset" and args.email == "alice@example.com"
