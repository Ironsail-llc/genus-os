"""Vault tool handlers — get, set, list, delete secrets."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("vault_get")
async def _vault_get(args: dict, ctx: ToolContext) -> dict:
    import robothor.vault as vault

    value = await asyncio.to_thread(vault.get, args["key"], tenant_id=ctx.tenant_id)
    if value is None:
        return {"error": f"Secret not found: {args['key']}"}
    return {"key": args["key"], "value": value}


@_handler("vault_set")
async def _vault_set(args: dict, ctx: ToolContext) -> dict:
    import robothor.vault as vault

    await asyncio.to_thread(
        vault.set,
        args["key"],
        args["value"],
        category=args.get("category", "credential"),
        tenant_id=ctx.tenant_id,
    )
    return {"success": True, "key": args["key"]}


@_handler("vault_list")
async def _vault_list(args: dict, ctx: ToolContext) -> dict:
    import robothor.vault as vault

    keys = await asyncio.to_thread(
        vault.list, category=args.get("category"), tenant_id=ctx.tenant_id
    )
    return {"keys": keys, "count": len(keys)}


@_handler("vault_delete")
async def _vault_delete(args: dict, ctx: ToolContext) -> dict:
    import robothor.vault as vault

    deleted = await asyncio.to_thread(vault.delete, args["key"], tenant_id=ctx.tenant_id)
    return {"success": deleted, "key": args["key"]}
