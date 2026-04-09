"""Tenant management CLI — create, list, and inspect tenants."""

from __future__ import annotations

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

    print("Usage: robothor tenant {create|list|status}")
    return 1


def _cmd_create(args: argparse.Namespace) -> int:
    """Create a new tenant with optional Telegram user binding."""
    from robothor.crm.dal import create_tenant_with_user, get_tenant
    from robothor.memory.blocks import seed_blocks_for_tenant

    tenant_id = args.id
    display_name = args.name
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

    print(f"Tenant: {tenant['id']}")
    print(f"  Name:   {tenant.get('display_name', '-')}")
    print(f"  Active: {tenant.get('active', True)}")
    print(f"  Parent: {tenant.get('parent_tenant_id') or '-'}")

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
