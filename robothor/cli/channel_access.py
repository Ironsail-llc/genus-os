"""``genus channel access`` — who may reach this instance, from the operator's shell.

The other half of :mod:`robothor.cli.channel`, and a separate module on purpose:
``channel.py`` is about what can *deliver*, this is about who may *drive*. They
share the ``genus channel`` parser through one function call —
:func:`add_access_parser` — so the two land independently without either owning
the other's sub-parser block.

``approve`` and ``deny`` are two of only two callers the pairing DAL accepts.
The other is the operator-gated bridge router; nothing reachable from a channel
is on that list, which is what makes a six-character code safe to send to a
stranger. This one qualifies because running it means already having a shell on
the box, and it says so in the ``actor`` it passes: ``cli:<user>``. An ``actor``
that did not carry that prefix would be refused by the DAL — the check is not
here, and not duplicated here.

Nothing prints a pairing code. ``access list`` shows that somebody is waiting
and when their code dies; the plaintext exists only in the reply the sender
received, which is the only place it can be and still be a proof of anything.
"""

from __future__ import annotations

import argparse  # noqa: TC003 - argparse.Namespace is used at runtime in signatures
import getpass
import sys
from typing import Any

from robothor.engine.channels import identities

__all__ = ["add_access_parser", "cmd_channel_access"]


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _actor() -> str:
    """Who this process is, in the form the DAL accepts.

    ``getpass.getuser()`` and not a configured name: the audit value has to be
    the account that actually ran the command, because that is the one a box's
    own logs can be reconciled against. It can fail on a container with no
    passwd entry, which is a reason to fall back — not a reason to drop the
    prefix that makes the actor acceptable at all.
    """
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry is not a reason to refuse
        user = "unknown"
    return f"cli:{user}"


def add_access_parser(
    channel_sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register ``access`` under an existing ``genus channel`` sub-parser.

    A function rather than a block inlined into ``robothor/cli/__init__.py`` so
    that the ``channel`` command and its ``access`` verbs could be written
    against each other without either being blocked on the other merging: the
    wiring is one call.
    """
    access = channel_sub.add_parser(
        "access",
        help="Who may drive this instance over a channel: pending pairings, approvals, revocations",
    )
    access_sub = access.add_subparsers(dest="access_command")

    listing = access_sub.add_parser(
        "list", help="Pending pairing requests and live identities for a channel"
    )
    listing.add_argument("name", help="Channel name, e.g. slack")

    approve = access_sub.add_parser("approve", help="Approve a pairing code and bind its sender")
    approve.add_argument("name", help="Channel name, e.g. slack")
    approve.add_argument("code", help="The code the sender was given")
    approve.add_argument("--user", default=None, help="Bind to this user id")
    approve.add_argument("--email", default=None, help="Bind to the account with this address")
    approve.add_argument(
        "--role",
        default="member",
        help="Role to grant. Only the roles pairing may grant are accepted: "
        + ", ".join(sorted(identities.PAIRABLE_ROLES)),
    )

    deny = access_sub.add_parser("deny", help="Refuse a pairing code, permanently")
    deny.add_argument("name", help="Channel name, e.g. slack")
    deny.add_argument("code", help="The code to refuse")

    revoke = access_sub.add_parser("revoke", help="Revoke a live identity on a channel")
    revoke.add_argument("name", help="Channel name, e.g. slack")
    revoke.add_argument("identity_id", metavar="identity-id", help="From `access list`")

    parser: argparse.ArgumentParser = access
    return parser


def cmd_channel_access(args: argparse.Namespace) -> int:
    """Dispatch ``genus channel access``."""
    verb = getattr(args, "access_command", None)
    if verb == "list":
        return _cmd_list(args)
    if verb == "approve":
        return _cmd_approve(args)
    if verb == "deny":
        return _cmd_deny(args)
    if verb == "revoke":
        return _cmd_revoke(args)
    print("Usage: genus channel access {list|approve|deny|revoke} <channel> [...]")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    channel = args.name
    try:
        pending = identities.list_pending(channel)
        bound = identities.list_identities(channel)
    except Exception as exc:  # noqa: BLE001 - a broken DB is a message, not a traceback
        _err(f"Could not read channel access for {channel}: {exc}")
        return 1

    print(f"Pending pairings ({len(pending)}):")
    for row in pending:
        named = "named" if row.get("display_name_present") else "unnamed"
        print(f"  {row['id']}  expires {row['expires_at']}  ({named})")
    if not pending:
        print("  (none)")

    print(f"\nIdentities ({len(bound)}):")
    for row in bound:
        print(
            f"  {row['id']}  {row['user_id']}  {row.get('role', '')}  "
            f"{row.get('display_name') or '(no name)'}  paired {row['paired_at']}"
        )
    if not bound:
        print("  (none)")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    if args.role not in identities.PAIRABLE_ROLES:
        _err(
            f"Refusing --role {args.role}: pairing may only grant "
            f"{', '.join(sorted(identities.PAIRABLE_ROLES))}. A privileged role is granted "
            "with `genus user`, not by approving a message from a stranger."
        )
        return 2
    if bool(args.user) == bool(args.email):
        _err("Name exactly one of --user or --email.")
        return 2

    try:
        identity = identities.approve_pairing(
            args.code,
            actor=_actor(),
            channel=args.name,
            user_id=args.user,
            email=args.email,
            role=args.role,
        )
    except identities.PairingError as exc:
        _err(f"Not approved: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _err(f"Not approved: {exc}")
        return 1

    print(f"Approved. {args.name} identity {identity['id']} bound as {args.role}.")
    return 0


def _cmd_deny(args: argparse.Namespace) -> int:
    try:
        identities.deny_pairing(args.code, actor=_actor(), channel=args.name)
    except identities.PairingError as exc:
        _err(f"Not denied: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _err(f"Not denied: {exc}")
        return 1

    print(f"Denied. That code can never be approved, on {args.name} or anywhere else.")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    try:
        revoked = identities.revoke(args.identity_id, actor=_actor())
    except Exception as exc:  # noqa: BLE001
        _err(f"Not revoked: {exc}")
        return 1

    if not revoked:
        _err(f"No live identity {args.identity_id} on {args.name}.")
        return 1
    print("Revoked. That sender may pair again; nothing bars the native id.")
    return 0
