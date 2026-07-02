"""Knowledge Vault tool handlers — verbatim store/search/get.

Gated by ``ROBOTHOR_RIP_12_ENABLED``. Distinct from the secrets-vault
``vault_*`` tools: these operate on ``memory_vault`` (searchable verbatim
reference data), not ``vault_secrets``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from robothor.engine.feature_flags import is_rip_enabled

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_RIP = 12
_DISABLED = {"error": "knowledge vault disabled (set ROBOTHOR_RIP_12_ENABLED=1)"}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("memory_vault_store")
async def _vault_store(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.vault import store_vault_entry

    try:
        entry_id = await store_vault_entry(
            caption=args.get("caption", ""),
            value=args.get("value", ""),
            entry_type=args.get("entry_type", "contact_info"),
            sensitivity=args.get("sensitivity", "medium"),
            source=args.get("source", "user_provided"),
            tenant_id=ctx.tenant_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"id": entry_id, "caption": args.get("caption", "")}


@_handler("memory_vault_search")
async def _vault_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.vault import search_vault

    results = await search_vault(
        args.get("query", ""),
        entry_type=args.get("entry_type") or None,
        limit=args.get("limit", 5),
        tenant_id=ctx.tenant_id,
    )
    # Never expose the value here — caption matches only.
    return {
        "results": [
            {
                "id": r["id"],
                "caption": r["caption"],
                "entry_type": r["entry_type"],
                "sensitivity": r["sensitivity"],
                "similarity": r["similarity"],
            }
            for r in results
        ]
    }


@_handler("memory_vault_get")
async def _vault_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.vault import get_vault_value

    return await asyncio.to_thread(
        get_vault_value,
        int(args.get("id", 0)),
        tenant_id=ctx.tenant_id,
        run_id=str(getattr(ctx, "run_id", "") or "") or None,
        agent_id=getattr(ctx, "agent_id", None),
    )
