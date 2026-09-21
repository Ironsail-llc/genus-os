"""Narrow native sales tools. Human approval is only on the bridge API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from robothor.constants import SANDBOX_DENIAL_PREFIX
from robothor.operations.store import Conflict
from robothor.sales.service import Sales
from robothor.sales.tool_schemas import CONTRACTS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def handler(name: str) -> Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]:
    async def call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            parsed = CONTRACTS[name][0].model_validate(args)
        except ValidationError:
            return {"error": "Invalid sales tool arguments"}
        if ctx.is_benchmark and name in {
            "sales_discover",
            "sales_propose_email",
            "sales_process_queue",
            "sales_research_parallel",
            "sales_create_request",
        }:
            return {"error": f"{SANDBOX_DENIAL_PREFIX} Sales writes disabled in benchmarks"}
        if not ctx.tenant_id:
            return {"error": "Authenticated tenant required"}
        if name == "sales_research_parallel":
            from robothor.sales.research_fanout import delegate_research

            try:
                return await delegate_research(parsed.buying_case, ctx)
            except (Conflict, ValueError):
                return {
                    "error": "Research delegation was unavailable or did not return a complete valid bundle"
                }
        if name == "sales_process_queue" and (
            not ctx.agent_id.startswith("workflow:")
            or getattr(ctx, "user_id", "") != "service:" + ctx.agent_id
            or getattr(ctx, "user_role", "") != "service"
        ):
            return {"error": "Sales queue execution requires a native service workflow identity"}
        service = Sales(ctx.tenant_id)
        try:
            if name == "sales_get_report":
                from robothor.sales.reporting import Reports

                reports = Reports(service)
                return (
                    await asyncio.to_thread(reports.get, str(parsed.report_id))
                    if parsed.report_id
                    else await asyncio.to_thread(reports.latest)
                )
            if name in {"sales_create_request", "sales_get_request", "sales_get_workspace"}:
                from robothor.sales.requests import Requests

                requests = Requests(service)
                if name == "sales_get_workspace":
                    return await asyncio.to_thread(requests.workspace)
                if name == "sales_create_request":
                    return await asyncio.to_thread(requests.create, parsed, "agent:" + ctx.agent_id)
                return await asyncio.to_thread(requests.get, str(parsed.request_id))
            if name == "sales_process_queue":
                from robothor.sales.queue import QueueDriver

                return await QueueDriver(service).tick(
                    parsed.stage, ctx.agent_id.removeprefix("workflow:")
                )
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
