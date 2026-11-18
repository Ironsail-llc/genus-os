"""``org_overview`` -- how every tenant this run may reach is doing, in one call.

The rollup is ``robothor.tenancy.rollup.tenant_rollup``; this handler decides
WHICH tenants it covers (``cross_tenant.reachable_tenants``: the run's own,
plus its descendants only for a top-level, non-benchmark run that was granted
them) and audits every other tenant it read. Read-only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

#: What a run that reaches no other tenant is told, instead of an empty list
#: it might read as "there are no other companies".
NO_REACH_NOTE = (
    "This run can see only its own tenant. Other tenants are reachable only from a "
    "top-level run by an owner or admin of a parent tenant, or by an unattended "
    "agent the parent has granted child access to."
)


async def _org_overview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.cross_tenant import audit_cross_tenant, reachable_tenants
    from robothor.tenancy.rollup import tenant_rollup

    tenants = reachable_tenants(ctx)  # in-memory: the session registry
    result: dict[str, Any] = await asyncio.to_thread(tenant_rollup, tenants)
    for target in tenants[1:]:
        audit_cross_tenant(
            home_tenant=ctx.tenant_id,
            target_tenant=target,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            tool="org_overview",
            outcome="ok",
        )
    if len(tenants) == 1:
        result["note"] = NO_REACH_NOTE
    return result


HANDLERS["org_overview"] = _org_overview
