"""Tenant management CLI — create, list, inspect, claim SSO domains, move accounts."""

from __future__ import annotations

import getpass
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def cmd_tenant(args: argparse.Namespace) -> int:
    """Dispatch tenant subcommands."""
    subcmd = getattr(args, "tenant_command", None)
    if subcmd == "create":
        return _cmd_create(args)
    if subcmd == "list":
        return _cmd_list()
    if subcmd == "status":
        return _cmd_status(args)
    if subcmd == "set-domains":
        return _cmd_set_domains(args)
    if subcmd == "move-account":
        return _cmd_move_account(args)

    print("Usage: robothor tenant {create|list|status|set-domains|move-account}")
    return 1


def _cmd_create(args: argparse.Namespace) -> int:
    """Create a new tenant with optional Telegram user binding."""
    from robothor.crm.dal import create_tenant_with_user, get_tenant
    from robothor.memory.blocks import seed_blocks_for_tenant

    tenant_id = args.id
    display_name = args.name or tenant_id
    telegram_user_id = getattr(args, "telegram_user_id", None)
    parent = getattr(args, "parent", None)

    # Check if tenant already exists
    existing = get_tenant(tenant_id)
    if existing:
        print(f"Error: Tenant '{tenant_id}' already exists.", file=sys.stderr)
        return 1

    if telegram_user_id:
        create_tenant_with_user(
            tenant_id=tenant_id,
            display_name=display_name,
            parent_tenant_id=parent,
            telegram_user_id=telegram_user_id,
            user_display_name=display_name,
        )
    else:
        from robothor.crm.dal import create_tenant

        create_tenant(
            tenant_id=tenant_id,
            display_name=display_name,
            parent_tenant_id=parent,
        )
        seed_blocks_for_tenant(tenant_id)

    print(f"Tenant '{tenant_id}' created successfully.")
    if telegram_user_id:
        print(f"  Telegram user {telegram_user_id} linked.")
    print("  Memory blocks seeded.")
    return 0


def _cmd_list() -> int:
    """List all tenants."""
    from robothor.crm.dal import list_tenants

    tenants = list_tenants(active_only=False)
    if not tenants:
        print("No tenants found.")
        return 0

    print(f"{'ID':<25} {'Name':<30} {'Active':<8} {'Parent'}")
    print("-" * 80)
    for t in tenants:
        active = "yes" if t.get("active", True) else "no"
        parent = t.get("parent_tenant_id") or "-"
        print(f"{t['id']:<25} {t.get('display_name', ''):<30} {active:<8} {parent}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Show tenant details, memory stats, and recent run counts."""
    from robothor.crm.dal import get_tenant

    tenant_id = args.tenant_id
    tenant = get_tenant(tenant_id)
    if not tenant:
        print(f"Tenant '{tenant_id}' not found.", file=sys.stderr)
        return 1

    from robothor.crm.tenant_domains import sso_domains_of

    # get_tenant returns the API shape (camelCase keys).
    print(f"Tenant: {tenant['id']}")
    print(f"  Name:   {tenant.get('displayName') or '-'}")
    print(f"  Active: {tenant.get('active', True)}")
    print(f"  Parent: {tenant.get('parentTenantId') or '-'}")
    print(f"  SSO domains: {', '.join(sso_domains_of(tenant)) or '-'}")

    # Memory block stats
    try:
        from robothor.memory.blocks import list_blocks

        blocks = list_blocks(tenant_id=tenant_id)
        block_list = blocks.get("blocks", [])
        print(f"\n  Memory blocks: {len(block_list)}")
        for b in block_list:
            size = b.get("size", 0)
            written = b.get("last_written_at") or "never"
            print(f"    {b['name']:<25} {size:>5} chars  (last: {written})")
    except Exception as e:
        print(f"\n  Memory blocks: error ({e})")

    # Recent run counts
    try:
        from robothor.db import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM agent_runs "
                "WHERE tenant_id = %s AND created_at > NOW() - interval '7 days'",
                (tenant_id,),
            )
            count = cur.fetchone()[0]
            print(f"\n  Runs (last 7d): {count}")
    except Exception as e:
        print(f"\n  Runs: error ({e})")

    return 0


def _cmd_set_domains(args: argparse.Namespace) -> int:
    """Replace the email domains a tenant claims for SSO provisioning."""
    from robothor.crm import dal
    from robothor.crm.tenant_domains import (
        InvalidSsoDomainError,
        SsoDomainConflictError,
        normalize_sso_domains,
    )

    tenant = dal.get_tenant(args.tenant_id)
    if not tenant:
        print(f"Tenant '{args.tenant_id}' not found.", file=sys.stderr)
        return 1
    try:
        domains = normalize_sso_domains(list(args.domains))
        settings = {**(tenant.get("settings") or {}), "sso_domains": domains}
        ok = dal.update_tenant(args.tenant_id, settings=settings)
    except (InvalidSsoDomainError, SsoDomainConflictError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"Error: could not update tenant '{args.tenant_id}'.", file=sys.stderr)
        return 1
    print(f"Tenant '{args.tenant_id}' SSO domains: {', '.join(domains) or '(none)'}")
    return 0


def _cmd_move_account(args: argparse.Namespace) -> int:
    """Move one account, and every row linked to it, to another tenant."""
    from robothor.auth import accounts

    found = accounts.accounts_for_email(args.email)
    if args.from_tenant:
        found = [a for a in found if a["tenant_id"] == args.from_tenant]
    if not found:
        where = f" in tenant '{args.from_tenant}'" if args.from_tenant else ""
        print(f"Error: no account for {args.email}{where}.", file=sys.stderr)
        return 1
    if len(found) > 1:
        tenants = ", ".join(str(a["tenant_id"]) for a in found)
        print(
            f"Error: {args.email} has an account in more than one tenant ({tenants}); "
            "choose one with --from <tenant>.",
            file=sys.stderr,
        )
        return 1

    try:
        report = accounts.move_account(
            str(found[0]["id"]),
            args.to,
            actor=f"cli:{getpass.getuser()}",
            role=args.role,
            dry_run=args.dry_run,
            detach_person=getattr(args, "detach_person", False),
        )
    except accounts.AccountMoveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    verb = "Would move" if report.dry_run else "Moved"
    print(f"{verb} {report.email} from '{report.from_tenant}' to '{report.to_tenant}'")
    if report.role_after != report.role_before:
        print(f"  Role: {report.role_before} -> {report.role_after}")
    for table, n in report.counts.items():
        print(f"  {table:<25} {n}")
    if report.company_unlinked:
        print(
            f"  Company unlinked: {report.company_unlinked} stays in '{report.from_tenant}'; "
            "the moved contact no longer points at it"
        )
    if report.new_person_id:
        print(f"  New CRM contact: {report.new_person_id} (the old one stays in the source)")
    if not report.dry_run:
        print(f"  Sessions revoked: {report.sessions_revoked}")
    return 0
