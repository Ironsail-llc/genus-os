"""Reaching another tenant from a tool call: who may, how, and the audit row it leaves.

Production runs a tenant tree in one database: a parent tenant and its child
tenants, one engine per tenant, row-level security bound to the process
tenant. A run's ``accessible_tenant_ids`` (``run_identity.attach_tenant_access``)
names the tenants it may reach; this module is the one place that turns that
tuple into an answer for a tool call.

A call that carries a ``tenant`` argument is admitted (:func:`admit`) only when

1. the tool is on ``CROSS_TENANT_READS`` or ``CROSS_TENANT_WRITES``
   (``tools/constants.py``),
2. the run is a live, top-level, non-benchmark run (:func:`is_top_level_run`),
3. the target is in ``ctx.accessible_tenant_ids``,
4. for a write other than ``UNATTENDED_CROSS_TENANT_WRITES`` (creating a task
   or a notification), the caller is the verified owner -- judged against the
   run's OWN context, because ``is_verified_owner`` checks the role in
   ``ctx.tenant_id`` and would fail against the swapped one.

A write may carry ``dry_run: true``: it is admitted exactly as the real call,
audited as a dry run, and answered by :func:`dry_run` with what would change;
the handler never runs. ``dry_run`` on an allowlisted write WITHOUT ``tenant``
is a dry run in the run's own tenant, never a real write. A ``dry_run`` that
is present but not a JSON boolean (``"true"``, ``1``, ``null``) is refused and
runs nothing: neither a real write nor a preview is guessed from it.

A tool that declares ``tenant`` in its OWN schema -- an external MCP server's
or a plugin's -- is none of this module's business: its ``tenant`` and
``dry_run`` reach it untouched (:func:`_argument_owner`).

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
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

#: The audit event every cross-tenant call writes.
CROSS_TENANT_EVENT = "agent.cross_tenant_call"

#: The argument a call names its target tenant with.
TENANT_ARG = "tenant"

#: The argument that turns an allowlisted write into a description of itself.
DRY_RUN_ARG = "dry_run"

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
    dry_run: bool = False


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
    from robothor.engine.run_identity import confined_to_own_tenant
    from robothor.engine.session_registry import lookup

    session = lookup(ctx.run_id) if ctx.run_id else None
    return session is not None and not confined_to_own_tenant(session.run)


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


#: The longest tenant id a call may name. Tenant creation enforces no pattern,
#: so the shape check is a type and a length: anything else is no tenant id.
MAX_TENANT_ID = 128


def _tenant_target(raw: Any) -> str | None:
    """The ``tenant`` argument as a tenant id: ``""`` for none (absent, null or
    blank), ``None`` for a value that cannot be one (not a string, too long, or
    carrying a control character)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return None
    target = raw.strip()
    if len(target) > MAX_TENANT_ID or not target.isprintable():
        return None
    return target


def audit_answered(name: str, args: dict[str, Any], ctx: ToolContext, outcome: str) -> None:
    """Audit an admitted crossing answered WITHOUT its handler running.

    The repeat guard (``outcome="repeat"``) and the effect ledger's replay
    (``outcome="replay"``) hand back a child's data from an earlier call; that
    is still a crossing. Nothing for a call in the run's own tenant, and nothing
    when ``tenant`` is the tool's own parameter: the effect ledger replays an
    external tool's writes too, and no other tenant was entered for those.
    """
    target = _tenant_target(args.get(TENANT_ARG))
    if not target or target == ctx.tenant_id:
        return
    if _argument_owner(name) == "tool":
        return
    audit_cross_tenant(
        home_tenant=ctx.tenant_id,
        target_tenant=target,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        tool=name,
        outcome=outcome,
    )


def _bad_dry_run(name: str) -> dict[str, Any]:
    from robothor.engine.refusal import refusal

    return refusal(
        f"'{name}' was not run: dry_run must be true or false.",
        gate="policy",
        name="dry_run_boolean",
        unlock="Send dry_run as the boolean true to preview, or false (or leave it out) to run.",
    )


def _argument_owner(name: str) -> str | None:
    """Who gives ``tenant`` and ``dry_run`` their meaning on a call to ``name``.

    ``"engine"``: an allowlisted built-in; :func:`admit` reads them. ``"tool"``:
    the tool's OWN registered schema declares ``tenant`` -- an external MCP
    server's tool or a plugin's -- so both reach it untouched, never refused,
    stripped or audited as a crossing. ``stamp_tenant_arguments`` adds the
    engine's property only to allowlisted names, and an adapter's schema is the
    server's own, so ``tenant`` in any other registered schema was declared by
    the tool itself. ``None``: neither; a ``tenant`` argument is refused, since
    the handler would ignore it and answer about the home tenant.
    """
    from robothor.engine.tools import get_registry
    from robothor.engine.tools.constants import CROSS_TENANT_READS, CROSS_TENANT_WRITES

    registry = get_registry()
    adapter = registry.get_adapter_route(name) is not None
    if not adapter and name in CROSS_TENANT_READS | CROSS_TENANT_WRITES:
        return "engine"
    if TENANT_ARG in registry.declared_parameters(name):
        return "tool"
    return None


def admit(name: str, args: dict[str, Any], ctx: ToolContext) -> Crossing | dict[str, Any] | None:
    """Decide a call's tenant. ``None``: no ``tenant`` argument, run as always.

    A dict is the refusal to return; nothing may run. A :class:`Crossing`
    carries the handler's arguments with ``tenant`` (and, for a write,
    ``dry_run``) removed.
    """
    from robothor.engine.tools.constants import (
        CROSS_TENANT_WRITES,
        UNATTENDED_CROSS_TENANT_WRITES,
    )

    if TENANT_ARG not in args and DRY_RUN_ARG not in args:
        return None
    owner = _argument_owner(name)
    if owner == "tool":
        return None
    target = _tenant_target(args.get(TENANT_ARG))
    if target is None:
        # Malformed: answered exactly as an unreachable tenant, the audited
        # value cut to a tenant id's length.
        return _refuse(
            name,
            str(args.get(TENANT_ARG))[:MAX_TENANT_ID],
            ctx,
            UNREACHABLE,
            gate="role",
            gate_name="cross_tenant_reach",
            unlock=_UNREACHABLE_UNLOCK,
        )
    is_write = owner == "engine" and name in CROSS_TENANT_WRITES
    if is_write and DRY_RUN_ARG in args and not isinstance(args[DRY_RUN_ARG], bool):
        # "true", 1, null: never guessed at, in either direction.
        return _bad_dry_run(name)
    dry = is_write and args.get(DRY_RUN_ARG) is True
    if TENANT_ARG not in args and not dry:
        return None
    drop = {TENANT_ARG, DRY_RUN_ARG} if is_write else {TENANT_ARG}
    stripped = {k: v for k, v in args.items() if k not in drop}
    if not target:
        return Crossing(None, stripped, dry)

    if owner is None:
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
        return Crossing(None, stripped, dry)
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
    if is_write and name not in UNATTENDED_CROSS_TENANT_WRITES:
        from robothor.engine.owner_gate import is_verified_owner

        # The ORIGINAL context: the owner is the owner of THIS run's tenant.
        if not is_verified_owner(ctx):
            return _refuse(
                name,
                target,
                ctx,
                f"Only the verified owner may use '{name}' in another tenant.",
                gate="role",
                gate_name="cross_tenant_owner",
                unlock=(
                    "Create a task in that tenant for its team instead (create_task with "
                    "`tenant`), or ask the owner to make this change."
                ),
            )
    return Crossing(target, stripped, dry)


#: The record each update tool changes, and the DAL getter that reads it.
_UPDATE_RECORDS: dict[str, tuple[str, str]] = {
    "update_task": ("task", "get_task"),
    "update_note": ("note", "get_note"),
}


async def dry_run(name: str, crossing: Crossing, ctx: ToolContext) -> dict[str, Any]:
    """What an admitted write WOULD do, with nothing changed. Audited as a dry run."""
    import asyncio

    from robothor.crm.tool_ids import id_argument_error

    target = crossing.target or ctx.tenant_id
    args = crossing.args
    result: dict[str, Any] = {"dry_run": True, "tenant": target, "tool": name}
    bad = id_argument_error(name, args)
    if bad is not None:
        result.update(bad)
    elif name in _UPDATE_RECORDS:
        kind, getter = _UPDATE_RECORDS[name]
        record_id = str(args.get("id") or "")
        current = await asyncio.to_thread(
            _current_record, getter, record_id, target, _data_scope(ctx)
        )
        if current is None:
            result["error"] = f"No {kind} {record_id} in that tenant; nothing would change."
        else:
            result["changes"] = {
                field: {"from": current.get(field), "to": value}
                for field, value in args.items()
                if field != "id" and current.get(field) != value
            }
            result["summary"] = (
                f"Would update {kind} {record_id}: {len(result['changes'])} field(s)."
            )
    else:
        result["would"] = dict(args)
        result["summary"] = f"Would run {name} in tenant {target}."
    if crossing.target is not None:
        audit_cross_tenant(
            home_tenant=ctx.tenant_id,
            target_tenant=target,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            tool=name,
            outcome="error" if "error" in result else "ok",
            dry_run=True,
            reason=str(result.get("error") or ""),
        )
    return result


def _data_scope(ctx: ToolContext) -> Any:
    """The row scope the real ``get_task`` / ``get_note`` handlers read with."""
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import scope_for_query

    return scope_for_query(data_scoping_mode(), ctx.identity)


def _current_record(
    getter: str, record_id: str, tenant_id: str, scope: Any
) -> dict[str, Any] | None:
    """The record a dry-run update would change, read in its tenant's scope and
    filtered by the caller's data scope: a row the caller may not read is
    answered exactly as one that does not exist."""
    from robothor.crm import dal

    with _scope(tenant_id):
        found: dict[str, Any] | None = getattr(dal, getter)(
            record_id, tenant_id=tenant_id, scope=scope
        )
    return found


@contextmanager
def _scope(tenant_id: str | None) -> Iterator[None]:
    """The ONE place this module binds connections to another tenant.

    Every caller is behind :func:`admit` (pinned by
    ``tests/test_cross_tenant_scope_callers.py``). ``None``: no change.
    """
    if tenant_id is None:
        yield
        return
    from robothor.db.connection import tenant_scope

    with tenant_scope(tenant_id):
        yield


def target_scope(crossing: Crossing | None) -> AbstractContextManager[None]:
    """The admitted call's target scope, or no change for an ordinary call."""
    return _scope(crossing.target if crossing is not None else None)


def target_context(ctx: ToolContext, crossing: Crossing | None) -> ToolContext:
    """The context the call runs as: the target tenant's, or ``ctx`` unchanged."""
    if crossing is None or crossing.target is None:
        return ctx
    return replace(ctx, tenant_id=crossing.target, accessible_tenant_ids=(crossing.target,))


def handler_args(args: dict[str, Any], crossing: Crossing | None) -> dict[str, Any]:
    """The arguments the handler sees: ``tenant`` and ``dry_run`` removed."""
    return args if crossing is None else crossing.args


async def run_handler(
    name: str,
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]],
    crossing: Crossing,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Run an admitted call: in the target's scope, as the target, and audited."""
    if crossing.target is None:
        return await handler(crossing.args, ctx)

    from robothor.engine.runtime import effects

    target = crossing.target
    scoped = target_context(ctx, crossing)
    # The durable effect record was reserved in the HOME tenant's ledger, keyed
    # by the arguments as sent (tenant included). A handler that reads it
    # (`task_options`, `note_options`) would refuse a record whose tenant is
    # not its context's, so it sees none: the write is journalled as an
    # ordinary result, not a host-verified readback.
    effect_token = effects.active_effect.set(None)
    outcome, reason = "error", ""
    try:
        with _scope(target):
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
    "description": "Optional: a child tenant's id (see org_overview) to run this call there.",
}


#: The optional argument every allowlisted WRITE's schema advertises.
DRY_RUN_PROPERTY: dict[str, Any] = {
    "type": "boolean",
    "description": "Optional: true describes what this write would change without making it.",
}


def stamp_tenant_arguments(schemas: dict[str, Any]) -> None:
    """Add the optional ``tenant`` property to every allowlisted tool's schema.

    Done once, where the built-in schemas are assembled, rather than in each
    schema literal: ``get_engine_schemas`` is size-capped, and half these tools
    are defined in ``robothor.api.mcp``. The parameters dict is COPIED: the MCP
    definitions are shared with the operator's own MCP server, which has no
    cross-tenant dispatch and must not advertise it.
    """
    from robothor.engine.tools.constants import CROSS_TENANT_READS, CROSS_TENANT_WRITES

    for name in CROSS_TENANT_READS | CROSS_TENANT_WRITES:
        fn = schemas.get(name, {}).get("function")
        if not isinstance(fn, dict):
            continue
        params = dict(fn.get("parameters") or {"type": "object"})
        added = {TENANT_ARG: TENANT_PROPERTY}
        if name in CROSS_TENANT_WRITES:
            added[DRY_RUN_ARG] = DRY_RUN_PROPERTY
        params["properties"] = {**(params.get("properties") or {}), **added}
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
