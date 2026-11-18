"""Reaching another tenant from a tool call: who may, and the audit row it leaves.

Production runs a tenant tree in one database: a parent tenant and its child
tenants, one engine per tenant, row-level security bound to the process
tenant. A run's ``accessible_tenant_ids`` (``run_identity.attach_tenant_access``)
names the tenants it may reach; this module is the one place that turns that
tuple into an answer for a tool call.

Reach beyond the run's own tenant is never granted to a sub-agent or a
benchmark run, whatever its context carries: a sub-agent inherits its parent's
user for attribution, and that is not authority (``owner_gate`` applies the
same rule). A call made outside a live run is treated the same way.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

#: The audit event every cross-tenant call writes.
CROSS_TENANT_EVENT = "agent.cross_tenant_call"


def is_top_level_run(ctx: ToolContext) -> bool:
    """Whether ``ctx`` belongs to a live, top-level, non-benchmark run.

    Fails closed: no session, a recorded parent, a nesting depth, the
    sub-agent trigger (a child of a ``tracking_disabled`` parent records no
    parent id) or a benchmark marker on either side all answer ``False``.
    """
    if ctx.is_benchmark:
        return False
    from robothor.engine.run_context import in_benchmark_run

    if in_benchmark_run():
        return False
    from robothor.engine.models import TriggerType
    from robothor.engine.session_registry import lookup

    session = lookup(ctx.run_id) if ctx.run_id else None
    if session is None:
        return False
    run = session.run
    return not (
        getattr(run, "parent_run_id", None)
        or int(getattr(run, "nesting_depth", 0) or 0) > 0
        or getattr(run, "trigger_type", None) == TriggerType.SUB_AGENT
        or getattr(run, "is_benchmark", False)
    )


def reachable_tenants(ctx: ToolContext) -> tuple[str, ...]:
    """The run's own tenant first, then every other tenant it may reach."""
    own = ctx.tenant_id
    if not is_top_level_run(ctx):
        return (own,)
    return (own, *[t for t in dict.fromkeys(ctx.accessible_tenant_ids) if t and t != own])


def audit_cross_tenant(
    *,
    home_tenant: str,
    target_tenant: str,
    user_id: str,
    agent_id: str,
    tool: str,
    outcome: str,
    dry_run: bool = False,
    reason: str = "",
) -> None:
    """One ``audit_log`` row for a call that reached (or tried to reach) another tenant.

    Written under the HOME tenant, the one whose agent acted, with the target
    named in ``target`` and ``details``. Queued to the engine's event writer,
    like every tool-call audit row (``dispatch._audit_tool_call``). Never raises.
    """
    try:
        from robothor.audit.logger import log_event
        from robothor.engine.event_writer import get_event_writer

        details: dict[str, Any] = {
            "home_tenant": home_tenant,
            "target_tenant": target_tenant,
            "user_id": user_id,
            "tool": tool,
            "outcome": outcome,
            "dry_run": dry_run,
        }
        if reason:
            details["reason"] = reason[:300]
        get_event_writer().submit(
            log_event,
            event_type=CROSS_TENANT_EVENT,
            action=tool,
            category="agent",
            actor=agent_id or "unknown",
            user_id=user_id,
            details=details,
            target=target_tenant,
            status=outcome,
            tenant_id=home_tenant,
        )
    except Exception:  # noqa: BLE001 - audit must not break the call it records
        logger.warning("cross-tenant audit row could not be queued", exc_info=True)
