"""Reaching another tenant from a tool call: who may, how, and the audit row it leaves.

Production runs a tenant tree in one database: a parent tenant and its child
tenants, one engine per tenant, row-level security bound to the process
tenant. A run's ``accessible_tenant_ids`` (``run_identity.attach_tenant_access``)
names the tenants it may reach; this module is the one place that turns that
tuple into an answer for a tool call.

A call that carries a ``tenant`` argument is admitted (:func:`admit`) only when

1. the tool is on ``CROSS_TENANT_READS`` (``tools/constants.py``),
2. the run is a live, top-level, non-benchmark run (:func:`is_top_level_run`),
3. the target is in ``ctx.accessible_tenant_ids``.

Then :func:`run_handler` strips the argument and runs the handler inside
``tenant_scope(target)`` with a copy of the context whose ``tenant_id`` is the
target. Everything else is refused with one sentence and runs nothing. A
tenant that does not exist and one this run may not reach are refused with
the SAME message, so the refusal cannot be used to discover tenants.

The scope is a ContextVar (``robothor.db.connection``). ``asyncio.to_thread``
copies the calling context, so a handler that checks out its connection in a
worker thread is bound to the target too; a connection checked out before the
call keeps its old binding, which is why the swap happens around the handler
and nowhere earlier. With ``ROBOTHOR_RLS_ENABLED`` unset the scope binds
nothing and isolation rests on the handler's own ``tenant_id = %s``, which is
why the context's ``tenant_id`` is swapped as well.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

#: The audit event every cross-tenant call writes.
CROSS_TENANT_EVENT = "agent.cross_tenant_call"

#: The argument a call names its target tenant with.
TENANT_ARG = "tenant"

#: One sentence for an unknown tenant AND an unreachable one: no existence oracle.
UNREACHABLE = "That tenant is not one this run can reach."
_UNREACHABLE_UNLOCK = (
    "Call org_overview to see the tenants this run can reach. Reaching a child "
    "tenant takes an owner or admin of its parent, or an agent the parent granted "
    "child access to."
)


@dataclass(frozen=True)
class Crossing:
    """An admitted call that named a tenant. ``target`` None: its own tenant."""

    target: str | None
    args: dict[str, Any]


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


def _refuse(
    name: str,
    target: str,
    ctx: ToolContext,
    message: str,
    *,
    gate: str,
    gate_name: str,
    unlock: str,
) -> dict[str, Any]:
    from robothor.engine.refusal import refusal

    audit_cross_tenant(
        home_tenant=ctx.tenant_id,
        target_tenant=target,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        tool=name,
        outcome="denied",
        reason=message,
    )
    return refusal(message, gate=gate, name=gate_name, unlock=unlock)


def admit(name: str, args: dict[str, Any], ctx: ToolContext) -> Crossing | dict[str, Any] | None:
    """Decide a call's tenant. ``None``: no ``tenant`` argument, run as always.

    A dict is the refusal to return; nothing may run. A :class:`Crossing`
    carries the handler's arguments with ``tenant`` removed.
    """
    target = args.get(TENANT_ARG)
    if TENANT_ARG not in args:
        return None
    stripped = {k: v for k, v in args.items() if k != TENANT_ARG}
    if not target:
        return Crossing(None, stripped)
    target = str(target).strip()

    from robothor.engine.tools.constants import CROSS_TENANT_READS

    if name not in CROSS_TENANT_READS:
        return _refuse(
            name,
            target,
            ctx,
            f"'{name}' cannot act in another tenant.",
            gate="allowlist",
            gate_name="cross_tenant_allowlist",
            unlock="Call it without `tenant`, or use a tool whose schema offers `tenant`.",
        )
    if target == ctx.tenant_id:
        return Crossing(None, stripped)
    if not is_top_level_run(ctx):
        return _refuse(
            name,
            target,
            ctx,
            "Only a top-level run can reach another tenant; sub-agent and benchmark runs cannot.",
            gate="policy",
            gate_name="cross_tenant_top_level",
            unlock="Report back and let the top-level run make this call.",
        )
    if target not in ctx.accessible_tenant_ids:
        return _refuse(
            name,
            target,
            ctx,
            UNREACHABLE,
            gate="role",
            gate_name="cross_tenant_reach",
            unlock=_UNREACHABLE_UNLOCK,
        )
    return Crossing(target, stripped)


async def run_handler(
    name: str,
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]],
    crossing: Crossing,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Run an admitted call: in the target's scope, as the target, and audited."""
    if crossing.target is None:
        return await handler(crossing.args, ctx)

    from robothor.db.connection import tenant_scope
    from robothor.engine.runtime import effects

    target = crossing.target
    scoped = replace(ctx, tenant_id=target, accessible_tenant_ids=(target,))
    # The durable effect record was reserved in the HOME tenant's ledger, keyed
    # by the arguments as sent (tenant included). A handler that reads it
    # (`task_options`, `note_options`) would refuse a record whose tenant is
    # not its context's, so it sees none: the write is journalled as an
    # ordinary result, not a host-verified readback.
    effect_token = effects.active_effect.set(None)
    outcome, reason = "error", ""
    try:
        with tenant_scope(target):
            result = await handler(crossing.args, scoped)
        if isinstance(result, dict) and result.get("error"):
            reason = str(result["error"])
        else:
            outcome = "ok"
        return result
    except BaseException as exc:
        reason = type(exc).__name__
        raise
    finally:
        effects.active_effect.reset(effect_token)
        audit_cross_tenant(
            home_tenant=ctx.tenant_id,
            target_tenant=target,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            tool=name,
            outcome=outcome,
            reason=reason,
        )


#: List tools whose rows gain an ``ageDays``, and the key the rows sit under.
AGED_LISTS: dict[str, str] = {
    "list_tasks": "tasks",
    "list_notes": "notes",
    "list_people": "people",
    "list_companies": "companies",
    "list_conversations": "conversations",
    "list_agent_runs": "runs",
    "list_agent_reviews": "reviews",
}

#: Where a row's age is measured from, first present wins.
_CREATED_KEYS = ("createdAt", "created_at", "started_at")


def annotate_ages(name: str, result: Any, *, now: datetime | None = None) -> Any:
    """Add ``as_of`` to a list result and ``ageDays`` to each of its rows.

    A raw ISO timestamp makes the model do date arithmetic, which it gets
    wrong; "12 days old" it reads correctly. Existing fields are never
    removed or changed. Anything that is not the expected shape is returned
    as it came.
    """
    key = AGED_LISTS.get(name)
    if key is None or not isinstance(result, dict) or "error" in result:
        return result
    rows = result.get(key)
    if not isinstance(rows, list):
        return result
    as_of = now or datetime.now(UTC)
    aged = [
        {**row, "ageDays": _age_days(row, as_of)} if isinstance(row, dict) else row for row in rows
    ]
    return {**result, key: aged, "as_of": as_of.isoformat()}


def _age_days(row: dict[str, Any], as_of: datetime) -> int | None:
    raw = next((row[k] for k in _CREATED_KEYS if row.get(k)), None)
    if raw is None:
        return None
    try:
        then = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return int((as_of - then).total_seconds() // 86400)


#: The optional argument every allowlisted tool's schema advertises.
TENANT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional. Run this call in another tenant (company) this run may reach: a "
        "child tenant's id, as org_overview lists them. Omit it for your own tenant. "
        "Refused for a tenant you cannot reach."
    ),
}


def stamp_tenant_arguments(schemas: dict[str, Any]) -> None:
    """Add the optional ``tenant`` property to every allowlisted tool's schema.

    Done once, where the built-in schemas are assembled, rather than in each
    schema literal: ``get_engine_schemas`` is size-capped, and half these tools
    are defined in ``robothor.api.mcp``. The parameters dict is COPIED: the MCP
    definitions are shared with the operator's own MCP server, which has no
    cross-tenant dispatch and must not advertise it.
    """
    from robothor.engine.tools.constants import CROSS_TENANT_READS

    for name in CROSS_TENANT_READS:
        fn = schemas.get(name, {}).get("function")
        if not isinstance(fn, dict):
            continue
        params = dict(fn.get("parameters") or {"type": "object"})
        params["properties"] = {**(params.get("properties") or {}), TENANT_ARG: TENANT_PROPERTY}
        fn["parameters"] = params


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
        from robothor.secrets.redaction import redact

        details: dict[str, Any] = {
            "home_tenant": home_tenant,
            "target_tenant": target_tenant,
            "user_id": user_id,
            "tool": tool,
            "outcome": outcome,
            "dry_run": dry_run,
        }
        if reason:
            details["reason"] = redact(reason[:4096])[:300]
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
