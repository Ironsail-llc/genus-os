"""Narrow native sales tools. Human approval is only on the bridge API."""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from robothor.constants import SANDBOX_DENIAL_PREFIX
from robothor.operations.store import Conflict
from robothor.sales.service import Sales
from robothor.sales.tool_schemas import CONTRACTS


def handler(name):
    async def call(args, ctx):
        try:
            parsed = CONTRACTS[name][0].model_validate(args)
        except ValidationError:
            return {"error": "Invalid sales tool arguments"}
        if ctx.is_benchmark and name in {"sales_discover", "sales_propose_email"}:
            return {"error": f"{SANDBOX_DENIAL_PREFIX} Sales writes disabled in benchmarks"}
        if not ctx.tenant_id:
            return {"error": "Authenticated tenant required"}
        service = Sales(ctx.tenant_id)
        try:
            if name == "sales_discover":
                return await asyncio.to_thread(service.discover, **parsed.model_dump())
            if name == "sales_get_prospect":
                return await asyncio.to_thread(service.get, parsed.prospect_id)
            if name == "sales_get_context":
                return await asyncio.to_thread(service.context, parsed.prospect_id)
            if name == "sales_propose_email":
                action = await asyncio.to_thread(service.draft, parsed.prospect_id, parsed.draft)
                return {"action_id": action, "status": "review"}
        except (Conflict, ValueError) as exc:
            return {"error": str(exc)}
        return {"error": "Unknown sales operation"}

    return call


HANDLERS = {name: handler(name) for name in CONTRACTS}
