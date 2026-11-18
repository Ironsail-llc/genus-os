"""``org_overview`` and ``org_watch`` -- every tenant this run may reach.

The rollup is ``robothor.tenancy.rollup.tenant_rollup``; these handlers decide
WHICH tenants it covers (``cross_tenant.reachable_tenants``: the run's own,
plus its descendants only for a top-level, non-benchmark run that was granted
them) and audit every other tenant they read. ``org_overview`` is read-only.
``org_watch`` (``robothor.tenancy.watch``) reports what changed since its last
call and, on a scheduled run only, stores what it saw in the run's own tenant.
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


#: What ``org_watch`` says when it watched only the run's own tenant.
#: Plain on purpose: the agent may pass it on to the owner as it stands. How
#: to grant the access is in docs/runbooks/TENANT_HIERARCHY.md.
WATCH_LIMITED_NOTE = (
    "This watch covered only this company: the parent has not granted this run "
    "access to its child companies, so their changes are not included."
)
WATCH_NOT_STORED_NOTE = "This watch was not stored, so the next scheduled watch still reports it."


async def _reachable_rollup(ctx: ToolContext, tool: str) -> tuple[tuple[str, ...], dict[str, Any]]:
    """The rollup for every tenant this run may reach, each other tenant audited."""
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.engine.tools.cross_tenant import audit_cross_tenant, reachable_tenants
    from robothor.identity.scope import scope_for_query
    from robothor.tenancy.rollup import tenant_rollup

    tenants = reachable_tenants(ctx)  # in-memory: the session registry
    rollup: dict[str, Any] = await asyncio.to_thread(
        tenant_rollup,
        tenants,
        # The caller's own tenant's task titles, as list_tasks would show them.
        own_tenant=ctx.tenant_id,
        own_scope=scope_for_query(data_scoping_mode(), ctx.identity),
    )
    for target in tenants[1:]:
        audit_cross_tenant(
            home_tenant=ctx.tenant_id,
            target_tenant=target,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            tool=tool,
            outcome="ok",
        )
    return tenants, rollup


async def _org_overview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tenants, result = await _reachable_rollup(ctx, "org_overview")
    if len(tenants) == 1:
        result["note"] = NO_REACH_NOTE
    return result


def _is_scheduled(ctx: ToolContext) -> bool:
    """Whether this call belongs to a live scheduled or system run.

    Found as ``cross_tenant.is_top_level_run`` finds the run: the session
    registry, by ``ctx.run_id``. No session answers ``False``.
    """
    from robothor.engine.run_identity import UNATTENDED_GRANT_TRIGGERS
    from robothor.engine.session_registry import lookup

    session = lookup(ctx.run_id) if ctx.run_id else None
    trigger = getattr(getattr(session, "run", None), "trigger_type", None)
    return session is not None and trigger in UNATTENDED_GRANT_TRIGGERS


async def _org_watch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """What changed since the last watch. Stores the snapshot only on a
    scheduled run (a trigger in ``UNATTENDED_GRANT_TRIGGERS``) without ``preview``.

    A person's run -- in chat, with or without an identity, with or without
    reach -- gets the report and never stores: it would swallow what the
    owner's next heartbeat should report. A scheduled run that reaches only its
    own tenant (``limited``) still stores: the snapshot keeps the tenants it did
    not see, and without storing it would announce the same problems every beat.

    Not read-only, so it is outside ``READONLY_TOOLS``: plan mode cannot call it
    (it uses ``org_overview``), and benchmark runs never get it.
    """
    from robothor.tenancy.watch import run_watch

    tenants, rollup = await _reachable_rollup(ctx, "org_watch")
    limited = len(tenants) == 1
    scheduled = _is_scheduled(ctx)
    result = await asyncio.to_thread(
        run_watch,
        ctx.tenant_id,
        rollup,
        daily=bool(args.get("daily", False)),
        store=scheduled and not bool(args.get("preview", False)),
    )
    result["limited"] = limited
    notes = [WATCH_LIMITED_NOTE] if limited else []
    if not scheduled:
        notes.append(WATCH_NOT_STORED_NOTE)
    if notes:
        result["note"] = " ".join(notes)
    return result


HANDLERS["org_overview"] = _org_overview
HANDLERS["org_watch"] = _org_watch
