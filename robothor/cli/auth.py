"""``robothor auth`` — user account / identity administration.

Phase A ships ``bootstrap``: seed the ``owner.yaml`` operator as the tenant's
``owner`` account so flipping ``GENUS_AUTH_ENFORCE`` on never locks the operator
out. Account/session DAL lives in :mod:`robothor.auth.accounts`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace


def cmd_auth(args: Namespace) -> int:
    if getattr(args, "auth_command", None) == "bootstrap":
        return _cmd_bootstrap(args)
    print("usage: robothor auth bootstrap [--json]")
    return 1


def _cmd_bootstrap(args: Namespace) -> int:
    from robothor.auth import accounts

    account = accounts.bootstrap_owner_account()
    if account is None:
        print("No operator configured (~/.robothor/owner.yaml) — nothing to bootstrap.")
        return 1

    if getattr(args, "json_output", False):
        print(json.dumps(account, default=str, indent=2))
    else:
        print(
            f"✓ Owner account ready: {account['email']} "
            f"(tenant={account['tenant_id']}, role={account['role']})"
        )
    return 0
