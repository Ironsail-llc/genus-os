"""``genus channel access`` — the operator's half of pairing, from their shell.

The CLI and the bridge are the only two callers ``approve_pairing`` accepts, and
they are accepted for different reasons: the bridge because ``require_operator``
already ran, the CLI because being able to run it is being on the box. That
second one only holds if the actor the CLI passes actually says so, which is
what most of these tests check — an ``actor`` that did not start with ``cli:``
would make the DAL's refusal ladder meaningless from this direction.

The rest is the ordinary CLI contract: the verbs parse, a privileged role is
refused before anything is written, and an unknown code exits non-zero rather
than printing nothing and returning 0.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pytest

from robothor.cli.channel_access import add_access_parser, cmd_channel_access

CODE = "ABC234"
IDENTITY_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def dal(monkeypatch):
    """Every DAL call the CLI can make, recorded rather than executed."""
    from robothor.cli import channel_access as mod

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(name: str, result: Any):
        def _fn(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            return result

        return _fn

    monkeypatch.setattr(
        mod.identities,
        "list_pending",
        _record(
            "list_pending", [{"id": "p-1", "expires_at": "soon", "display_name_present": True}]
        ),
    )
    monkeypatch.setattr(
        mod.identities,
        "list_identities",
        _record(
            "list_identities",
            [
                {
                    "id": IDENTITY_ID,
                    "user_id": "u-alice",
                    "display_name": "Alice",
                    "paired_at": "now",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        mod.identities, "approve_pairing", _record("approve_pairing", {"id": IDENTITY_ID})
    )
    monkeypatch.setattr(mod.identities, "deny_pairing", _record("deny_pairing", {"id": "p-1"}))
    monkeypatch.setattr(mod.identities, "revoke", _record("revoke", True))
    return calls


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="genus")
    sub = parser.add_subparsers(dest="command")
    channel = sub.add_parser("channel")
    channel_sub = channel.add_subparsers(dest="channel_command")
    add_access_parser(channel_sub)
    return parser.parse_args(argv)


# ── Parsing ─────────────────────────────────────────────────────────


def test_the_four_verbs_parse():
    assert _parse(["channel", "access", "list", "slack"]).access_command == "list"
    assert _parse(["channel", "access", "approve", "slack", CODE]).access_command == "approve"
    assert _parse(["channel", "access", "deny", "slack", CODE]).access_command == "deny"
    assert _parse(["channel", "access", "revoke", "slack", IDENTITY_ID]).access_command == "revoke"


def test_access_registers_under_the_existing_channel_parser():
    """C4 owns ``genus channel``; this adds one sub-parser to it rather than a
    second command, so ``list``/``verify``/``add`` keep working."""
    args = _parse(["channel", "access", "list", "slack"])
    assert args.command == "channel"
    assert args.channel_command == "access"
    assert args.name == "slack"


# ── The actor ───────────────────────────────────────────────────────


def test_approve_passes_a_cli_actor(dal):
    rc = cmd_channel_access(
        _parse(["channel", "access", "approve", "slack", CODE, "--user", "u-alice"])
    )

    assert rc == 0
    name, args, kwargs = dal[0]
    assert name == "approve_pairing"
    assert args[0] == CODE
    assert kwargs["actor"].startswith("cli:")
    assert kwargs["channel"] == "slack"
    assert kwargs["user_id"] == "u-alice"


def test_deny_and_revoke_pass_a_cli_actor(dal):
    cmd_channel_access(_parse(["channel", "access", "deny", "slack", CODE]))
    cmd_channel_access(_parse(["channel", "access", "revoke", "slack", IDENTITY_ID]))

    actors = [kwargs["actor"] for _, _, kwargs in dal]
    assert all(actor.startswith("cli:") for actor in actors)


def test_approve_by_email_is_accepted(dal):
    rc = cmd_channel_access(
        _parse(["channel", "access", "approve", "slack", CODE, "--email", "alice@example.com"])
    )

    assert rc == 0
    assert dal[0][2]["email"] == "alice@example.com"


# ── Refusals ────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_a_privileged_role_is_refused_before_anything_is_written(dal, role, capsys):
    rc = cmd_channel_access(
        _parse(["channel", "access", "approve", "slack", CODE, "--user", "u-alice", "--role", role])
    )

    assert rc != 0
    assert dal == []
    assert role in capsys.readouterr().err


def test_naming_neither_user_nor_email_is_refused(dal, capsys):
    rc = cmd_channel_access(_parse(["channel", "access", "approve", "slack", CODE]))

    assert rc != 0
    assert dal == []


def test_an_unknown_code_exits_non_zero(monkeypatch, capsys):
    from robothor.cli import channel_access as mod
    from robothor.engine.channels.identities import PairingCodeError

    def _boom(*a: Any, **kw: Any) -> Any:
        raise PairingCodeError("no live code for that value")

    monkeypatch.setattr(mod.identities, "approve_pairing", _boom)

    rc = cmd_channel_access(
        _parse(["channel", "access", "approve", "slack", CODE, "--user", "u-alice"])
    )

    assert rc == 1
    assert "no live code" in capsys.readouterr().err


def test_revoking_an_unknown_identity_exits_non_zero(monkeypatch):
    from robothor.cli import channel_access as mod

    monkeypatch.setattr(mod.identities, "revoke", lambda *a, **kw: False)

    rc = cmd_channel_access(_parse(["channel", "access", "revoke", "slack", IDENTITY_ID]))

    assert rc == 1


# ── Listing ─────────────────────────────────────────────────────────


def test_list_prints_pending_codes_and_identities_without_the_code(dal, capsys):
    rc = cmd_channel_access(_parse(["channel", "access", "list", "slack"]))

    out = capsys.readouterr().out
    assert rc == 0
    assert IDENTITY_ID in out
    assert CODE not in out
