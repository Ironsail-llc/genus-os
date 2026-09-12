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

from robothor.cli import _invoked_name

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
    if command == "setup-link":
        return _cmd_setup_link(args)
    print(
        f"usage: {_invoked_name()} auth "
        "{bootstrap,grant-binding,grants,revoke-binding,setup-link} [--json]"
    )
    return 1


def _cmd_setup_link(args: Namespace) -> int:
    """Mint a fresh first-run link for a box nobody can re-run ``init`` on.

    ``genus init`` prints one at the end, but that link expires in half an hour
    and a headless install is often set up by someone who was not watching the
    terminal. This is the recovery path, and it is deliberately a LOCAL command:
    minting requires shell access to the box, which is the only credential the
    wizard's own gate can rely on before an account exists.

    Refuses once an owner account exists. The routes the link reaches answer 404
    from that moment, so handing over a token that does nothing would send the
    operator hunting for a broken dashboard instead of the sign-in page they
    actually want.
    """
    from robothor import setup_token
    from robothor.settings.sources import workspace_path

    workspace = workspace_path()
    if workspace is None:
        print(
            "error: no workspace — set ROBOTHOR_WORKSPACE (or run from an "
            "initialised install) and try again",
            file=sys.stderr,
        )
        return 2

    if setup_token.setup_complete(workspace):
        print(
            "This instance already has an owner account, so first-run setup is over "
            "and /setup answers 404.",
            file=sys.stderr,
        )
        print(
            "  Sign in at the dashboard instead; `genus user password <email>` resets "
            "a forgotten one.",
            file=sys.stderr,
        )
        return 1

    ttl_raw = getattr(args, "ttl", None)
    ttl_seconds: int | None = None
    if ttl_raw:
        try:
            ttl_seconds = _parse_ttl(ttl_raw)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    from robothor.setup import helm_port

    port = helm_port()
    host = getattr(args, "host", None) or "127.0.0.1"
    token = setup_token.create_setup_token(workspace, ttl_seconds=ttl_seconds)
    effective_ttl = ttl_seconds if ttl_seconds is not None else setup_token.configured_ttl_seconds()
    url = setup_token.setup_link(host, port, token)

    if getattr(args, "json_output", False):
        # The URL carries the token, which is the point: this output is for an
        # operator's own terminal or a provisioning script, never for a log.
        print(json.dumps({"url": url, "expires_in_seconds": effective_ttl}, indent=2))
        return 0

    print("Open this once, within the window — it is single-use:")
    print(f"  {url}")
    print(f"  Expires in {effective_ttl // 60} minute(s).")
    if not setup_token.is_loopback_host(host):
        print("  If that address is not reachable from your browser, forward the port:")
        print(f"    {setup_token.port_forward_hint(host, port)}")
    return 0


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
