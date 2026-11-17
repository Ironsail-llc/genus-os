"""`genus effects`: an operator's view of writes whose outcome is unknown.

A write whose outcome is unknown fences a repeat of the same action until it is
settled (``robothor.engine.runtime.effects``). A read-back verifier settles it
when the tool has one; otherwise an operator who has checked the outside system
records what they found, with the reason, here.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TypeVar
from uuid import UUID

_Parser = TypeVar("_Parser", bound="argparse.ArgumentParser")

#: The flag spells it with a hyphen; the record keeps the engine's name.
_OUTCOMES = {"applied": "applied", "not-applied": "not_applied"}


def add_parser(subparsers: argparse._SubParsersAction[_Parser]) -> None:
    parser = subparsers.add_parser(
        "effects", help="List unresolved writes and record what happened to one"
    )
    parser.add_argument("--tenant", help="Tenant id (default: the default tenant)")
    commands = parser.add_subparsers(dest="effects_command", required=True)
    listing = commands.add_parser("list", help="Writes whose outcome is unknown, oldest first")
    resolve = commands.add_parser(
        "resolve", help="Record whether an unresolved write took effect, and why you know"
    )
    for command in (listing, resolve):
        # Accepted after the verb too; SUPPRESS keeps the parent's value when absent.
        command.add_argument("--tenant", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    resolve.add_argument("effect_id", help="The effect id the refusal or `list` names")
    resolve.add_argument("--outcome", required=True, choices=sorted(_OUTCOMES))
    resolve.add_argument("--reason", required=True, help="What you checked, kept with the record")
    resolve.add_argument(
        "--actor", default="operator:cli", help="Who is recording it (default: operator:cli)"
    )


def cmd_effects(args: argparse.Namespace) -> int:
    from robothor.constants import DEFAULT_TENANT
    from robothor.engine.runtime import effects

    tenant = getattr(args, "tenant", None) or DEFAULT_TENANT
    try:
        if args.effects_command == "list":
            print(json.dumps({"effects": effects.unresolved(tenant)}, default=str, indent=2))
            return 0
        effect_id = str(UUID(args.effect_id))
        resolved = effects.operator_resolve(
            tenant,
            effect_id,
            outcome=_OUTCOMES[args.outcome],
            actor=args.actor,
            reason=args.reason,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resolved:
        print(
            f"Effect {effect_id} is not unresolved in tenant {tenant} (already settled, still "
            "being sent, or owned by a goal): nothing changed",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"resolved": True, "effect_id": effect_id}))
    return 0
