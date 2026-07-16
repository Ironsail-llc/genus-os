"""``robothor auth`` — user account / identity administration.

``bootstrap`` seeds the ``owner.yaml`` operator as the tenant's ``owner``
account so flipping ``GENUS_AUTH_ENFORCE`` on never locks the operator out.
``grant-binding`` / ``grants`` / ``revoke-binding`` manage the one-shot SSO
binding grants that let an existing account (e.g. that bootstrapped owner) be
bound to an IdP identity on its next verified sign-in — email equality alone
never binds. Account/session/grant DAL lives in :mod:`robothor.auth.accounts`.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

_TTL_SUFFIXES = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_ttl(value: str) -> int:
    """Parse ``45s`` / ``15m`` / ``2h`` / ``1d`` (bare digits = seconds) into
    seconds. Raises ValueError on anything non-positive or unrecognized."""
    value = value.strip()
    multiplier = 1
    if value and value[-1].lower() in _TTL_SUFFIXES:
        multiplier = _TTL_SUFFIXES[value[-1].lower()]
        value = value[:-1]
    if not value.isdigit():
        raise ValueError(f"invalid ttl: {value!r} (use e.g. 45s, 15m, 2h, 1d)")
    seconds = int(value) * multiplier
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    return seconds


def cmd_auth(args: Namespace) -> int:
    command = getattr(args, "auth_command", None)
    if command == "bootstrap":
        return _cmd_bootstrap(args)
    if command == "grant-binding":
        return _cmd_grant_binding(args)
    if command == "grants":
        return _cmd_grants(args)
    if command == "revoke-binding":
        return _cmd_revoke_binding(args)
    print("usage: robothor auth {bootstrap,grant-binding,grants,revoke-binding} [--json]")
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


def _cmd_grant_binding(args: Namespace) -> int:
    from robothor.auth import accounts
    from robothor.constants import DEFAULT_TENANT

    try:
        ttl_seconds = _parse_ttl(getattr(args, "ttl", None) or "15m")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        grant = accounts.create_binding_grant(
            tenant_id=getattr(args, "tenant", None) or DEFAULT_TENANT,
            email=args.email,
            ttl_seconds=ttl_seconds,
            reason=getattr(args, "reason", None) or "",
            issuer=getattr(args, "issuer", None) or None,
        )
    except accounts.GrantTargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json_output", False):
        print(json.dumps(grant, default=str, indent=2))
    else:
        print(
            f"✓ Binding grant {grant['id']} armed for {grant['email']} "
            f"(tenant={grant['tenant_id']}, expires {grant['expires_at']})"
        )
        print("  The next verified SSO sign-in with this email binds the account.")
    return 0


def _cmd_grants(args: Namespace) -> int:
    from robothor.auth import accounts
    from robothor.constants import DEFAULT_TENANT

    grants = accounts.list_binding_grants(
        getattr(args, "tenant", None) or DEFAULT_TENANT,
        include_inactive=bool(getattr(args, "include_inactive", False)),
    )
    if getattr(args, "json_output", False):
        print(json.dumps(grants, default=str, indent=2))
        return 0
    if not grants:
        print("No binding grants.")
        return 0
    for grant in grants:
        state = grant.get("state") or (
            "revoked" if grant.get("revoked_at") else "used" if grant.get("used_at") else "pending"
        )
        line = f"{grant['id']}  {grant['email']}  {state}  expires {grant['expires_at']}"
        if grant.get("used_by_subject"):
            line += f"  bound-subject {grant['used_by_subject']}"
        print(line)
    return 0


def _cmd_revoke_binding(args: Namespace) -> int:
    import uuid

    from robothor.auth import accounts

    try:
        uuid.UUID(args.grant_id)
    except ValueError:
        print(f"error: {args.grant_id!r} is not a grant UUID", file=sys.stderr)
        return 2

    if accounts.revoke_binding_grant(args.grant_id, getattr(args, "tenant", None)):
        print(f"✓ Grant {args.grant_id} revoked.")
        return 0
    print(f"Grant {args.grant_id} not found or already used/revoked.")
    return 1
