"""CRM tool handlers — people, companies, notes, tasks, conversations, merge, metadata, notifications."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

# Every CRM mutator — refused when ctx.is_benchmark, unless the run is scoped
# to the benchmark sandbox tenant AND the tool is sandbox-safe (see
# _sandbox_write_allowed). Mirrors _GWS_MUTATING_TOOLS (handlers/gws.py): a
# benchmark run otherwise materializes fixture text as real crm_tasks and
# notifications, which the briefing then reports as genuine events. Reads
# (get_task, list_tasks, search_records, …) stay allowed so benchmark prompts
# can inspect state.
#
# The people/company/note writers were added 2026-08-21. Until then the gate
# covered only the task tools, so `create_person` and `update_person` had no
# handler-level guard at all — they were kept out of benchmark runs purely by
# the harness deny-list, which is computed once per suite. A task that seeded
# no fixtures therefore ran with the sandbox write tools while NOT being scoped
# to the sandbox tenant, and could have written people rows straight into the
# production tenant. Belt and braces, both fastened.
_CRM_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        # People / companies / notes.
        "create_person",
        "update_person",
        "delete_person",
        "create_company",
        "update_company",
        "delete_company",
        "create_note",
        "update_note",
        "delete_note",
        "merge_people",
        "merge_contacts",
        "merge_companies",
        # Tasks.
        "create_task",
        "update_task",
        "resolve_task",
        "delete_task",
        "approve_task",
        "reject_task",
        # Operator-facing / conversational.
        "send_notification",
        "ack_notification",
        "create_message",
        "toggle_conversation_status",
    }
)


def _sandbox_write_allowed(name: str, ctx: ToolContext) -> bool:
    """Whether a benchmark run may perform this CRM write.

    Only in the dedicated sandbox tenant, and only for the tools listed in
    ``benchmark_sandbox.SANDBOX_WRITE_TOOLS``. A benchmark run in ANY other
    tenant is refused exactly as before — the tenant is what makes the write
    safe, not the fact that it is a benchmark. ``send_notification`` reaches a
    human and so is never in that set, whatever tenant it is called from.
    """
    from robothor.engine.benchmark_sandbox import SANDBOX_WRITE_TOOLS, sandbox_tenant_id

    return name in SANDBOX_WRITE_TOOLS and ctx.tenant_id == sandbox_tenant_id()


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _CRM_MUTATING_TOOLS:

            async def gated(
                args: dict[str, Any],
                ctx: ToolContext,
                _fn: Callable[..., Any] = fn,
                _name: str = name,
            ) -> dict[str, Any]:
                if ctx.is_benchmark and not _sandbox_write_allowed(_name, ctx):
                    return {
                        "error": f"benchmark sandbox: {_name} writes are disabled",
                        "guard": "is_benchmark",
                    }
                return cast("dict[str, Any]", await _fn(args, ctx))

            HANDLERS[name] = gated
        else:
            HANDLERS[name] = fn
        return fn

    return decorator


# ── People ──


@_handler("create_person")
async def _create_person(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import create_person

    person_id = await asyncio.to_thread(
        create_person,
        args.get("firstName", ""),
        args.get("lastName", ""),
        args.get("email"),
        args.get("phone"),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"id": person_id, "firstName": args.get("firstName", "")}
        if person_id
        else {"error": "Failed to create person"}
    )


@_handler("get_person")
async def _get_person(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_person
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_identity_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    result = await asyncio.to_thread(
        get_person,
        args["id"],
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_identity_scope([result] if result else [], _obs_scope)
        log_would_drop(
            tool_name="get_person",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_people",
        )

    return result or {"error": "Person not found"}


@_handler("update_person")
async def _update_person(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import update_person

    pid = args.get("id", "")
    field_map = {
        "firstName": "first_name",
        "lastName": "last_name",
        "email": "email",
        "phone": "phone",
        "jobTitle": "job_title",
        "city": "city",
        "companyId": "company_id",
        "linkedinUrl": "linkedin_url",
        "avatarUrl": "avatar_url",
        "doNotContact": "do_not_contact",
    }
    kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
    ok = await asyncio.to_thread(update_person, pid, tenant_id=ctx.tenant_id, **kwargs)
    return {"success": ok, "id": pid}


@_handler("list_people")
async def _list_people(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_people
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_identity_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        list_people,
        search=args.get("search"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_identity_scope(results, _obs_scope)
        log_would_drop(
            tool_name="list_people",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_people",
        )

    return {"people": results, "count": len(results)}


@_handler("delete_person")
async def _delete_person(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import delete_person

    ok = await asyncio.to_thread(delete_person, args["id"], tenant_id=ctx.tenant_id)
    return {"success": ok, "id": args["id"]}


# ── Companies ──


@_handler("create_company")
async def _create_company(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import create_company

    company_id = await asyncio.to_thread(
        create_company,
        name=args.get("name", ""),
        domain_name=args.get("domainName"),
        employees=args.get("employees"),
        address_street1=args.get("addressStreet1"),
        address_street2=args.get("addressStreet2"),
        address_city=args.get("addressCity"),
        address_state=args.get("addressState"),
        address_postcode=args.get("addressPostcode"),
        address_country=args.get("addressCountry"),
        address=args.get("address"),
        linkedin_url=args.get("linkedinUrl"),
        ideal_customer_profile=args.get("idealCustomerProfile", False),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"id": company_id, "name": args.get("name", "")}
        if company_id
        else {"error": "Failed to create company"}
    )


@_handler("get_company")
async def _get_company(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_company

    return await asyncio.to_thread(get_company, args["id"], tenant_id=ctx.tenant_id) or {
        "error": "Company not found"
    }


@_handler("update_company")
async def _update_company(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import update_company

    cid = args.get("id", "")
    field_map = {
        "name": "name",
        "domainName": "domain_name",
        "employees": "employees",
        "addressStreet1": "address_street1",
        "addressStreet2": "address_street2",
        "addressCity": "address_city",
        "addressState": "address_state",
        "addressPostcode": "address_postcode",
        "addressCountry": "address_country",
        "address": "address",  # flat fallback — DAL maps to address_street1
        "linkedinUrl": "linkedin_url",
        "idealCustomerProfile": "ideal_customer_profile",
    }
    kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
    ok = await asyncio.to_thread(update_company, cid, tenant_id=ctx.tenant_id, **kwargs)
    return {"success": ok, "id": cid}


@_handler("list_companies")
async def _list_companies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_companies

    results = await asyncio.to_thread(
        list_companies,
        search=args.get("search"),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
    )
    return {"companies": results, "count": len(results)}


@_handler("delete_company")
async def _delete_company(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import delete_company

    ok = await asyncio.to_thread(delete_company, args["id"], tenant_id=ctx.tenant_id)
    return {"success": ok, "id": args["id"]}


# ── Notes ──


@_handler("create_note")
async def _create_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import create_note

    note_id = await asyncio.to_thread(
        create_note,
        title=args.get("title", ""),
        body=args.get("body", ""),
        person_id=args.get("personId"),
        company_id=args.get("companyId"),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"id": note_id, "title": args.get("title", "")}
        if note_id
        else {"error": "Failed to create note"}
    )


@_handler("get_note")
async def _get_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_note
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    result = await asyncio.to_thread(
        get_note,
        args["id"],
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope([result] if result else [], _obs_scope)
        log_would_drop(
            tool_name="get_note",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_notes",
        )

    return result or {"error": "Note not found"}


@_handler("list_notes")
async def _list_notes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_notes
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        list_notes,
        person_id=args.get("personId"),
        company_id=args.get("companyId"),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope(results, _obs_scope)
        log_would_drop(
            tool_name="list_notes",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_notes",
        )

    return {"notes": results, "count": len(results)}


@_handler("update_note")
async def _update_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import update_note

    nid = args.get("id", "")
    field_map = {
        "title": "title",
        "body": "body",
        "personId": "person_id",
        "companyId": "company_id",
    }
    kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
    ok = await asyncio.to_thread(update_note, nid, tenant_id=ctx.tenant_id, **kwargs)
    return {"success": ok, "id": nid}


@_handler("delete_note")
async def _delete_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import delete_note

    ok = await asyncio.to_thread(delete_note, args["id"], tenant_id=ctx.tenant_id)
    return {"success": ok, "id": args["id"]}


# ── Tasks ──


@_handler("create_task")
async def _create_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    import re as _re

    from robothor.crm.dal import create_task, find_task_by_dedup_key

    # Server-side dedup: check for existing task with any known dedup key
    body_text = args.get("body") or ""
    dedup_keys = ["threadId", "conversationId", "eventId", "escalationId"]
    for key in dedup_keys:
        match = _re.search(rf"{key}:\s*(\S+)", body_text)
        if match:
            existing = await asyncio.to_thread(
                find_task_by_dedup_key,
                key_name=key,
                key_value=match.group(1),
                include_recently_resolved=True,
                tenant_id=ctx.tenant_id,
            )
            if existing:
                return {
                    "id": existing["id"],
                    "title": existing["title"],
                    "deduplicated": True,
                }
            break  # Only check the first matching key

    # Author attribution precedence: explicit arg > context override > agent_id.
    # The context override lets the scout beat (running as agent_id='main')
    # file tasks attributed to 'scout' for CRM timeline clarity.
    author = args.get("createdByAgent") or ctx.task_author_override or ctx.agent_id
    task_id = await asyncio.to_thread(
        create_task,
        title=args.get("title", ""),
        body=args.get("body"),
        status=args.get("status", "TODO"),
        due_at=args.get("dueAt"),
        person_id=args.get("personId"),
        company_id=args.get("companyId"),
        assigned_to_agent=args.get("assignedToAgent"),
        created_by_agent=author,
        priority=args.get("priority", "normal"),
        tags=args.get("tags"),
        parent_task_id=args.get("parentTaskId"),
        requires_human=args.get("requiresHuman", False),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"id": task_id, "title": args.get("title", "")}
        if task_id
        else {"error": "Failed to create task"}
    )


@_handler("get_task")
async def _get_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_task
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    # Validate at the tool boundary: LLM-hallucinated placeholder ids
    # ("task_jkl012") used to reach the uuid-typed SQL parameter verbatim and
    # crash with psycopg2 InvalidTextRepresentation.
    task_id = args.get("id", "")
    try:
        uuid.UUID(str(task_id))
    except ValueError:
        return {
            "error": (
                f"invalid task id {task_id!r} — expected a UUID; "
                "use list_tasks or list_my_tasks to find real task ids"
            )
        }

    _mode = data_scoping_mode()
    result = await asyncio.to_thread(
        get_task,
        args["id"],
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope([result] if result else [], _obs_scope)
        log_would_drop(
            tool_name="get_task",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_tasks",
        )

    return result or {"error": "Task not found"}


@_handler("list_tasks")
async def _list_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_tasks
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        list_tasks,
        status=args.get("status"),
        person_id=args.get("personId"),
        assigned_to_agent=args.get("assignedToAgent"),
        created_by_agent=args.get("createdByAgent"),
        priority=args.get("priority"),
        tags=args.get("tags"),
        exclude_resolved=args.get("excludeResolved", True),
        requires_human=args.get("requiresHuman"),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope(results, _obs_scope)
        log_would_drop(
            tool_name="list_tasks",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_tasks",
        )

    return {"tasks": results, "count": len(results)}


@_handler("update_task")
async def _update_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import update_task

    tid = args.get("id", "")
    field_map = {
        "title": "title",
        "body": "body",
        "status": "status",
        "dueAt": "due_at",
        "personId": "person_id",
        "companyId": "company_id",
        "assignedToAgent": "assigned_to_agent",
        "priority": "priority",
        "tags": "tags",
        "resolution": "resolution",
        "requiresHuman": "requires_human",
    }
    kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
    # Author attribution for task history: context override > agent_id.
    changed_by = ctx.task_author_override or ctx.agent_id
    ok = await asyncio.to_thread(
        update_task, tid, changed_by=changed_by, tenant_id=ctx.tenant_id, **kwargs
    )
    return {"success": ok, "id": tid}


@_handler("delete_task")
async def _delete_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import delete_task

    ok = await asyncio.to_thread(delete_task, args["id"], tenant_id=ctx.tenant_id)
    return {"success": ok, "id": args["id"]}


@_handler("resolve_task")
async def _resolve_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Resolve a task. If the task body has a ``` ```accept … ``` ``` block,
    run it first and block the resolve when any command fails.

    Acceptance is non-gameable (deterministic shell commands, not LLM). If no
    block is present, behavior is unchanged — a bare resolve_task call.
    """
    from robothor.crm.dal import get_task, resolve_task
    from robothor.engine.thread_pool import parse_accept_block, run_accept

    task_id = args["id"]
    resolution = args.get("resolution", "")

    # Acceptance check — run deterministic block if present in body.
    # Never let an acceptance fetch error block a resolve; fall through.
    accept_result: dict[str, object] | None = None
    try:
        task = await asyncio.to_thread(get_task, task_id, tenant_id=ctx.tenant_id)
    except Exception:
        task = None
    if task and task.get("body"):
        commands = parse_accept_block(task["body"])
        if commands:
            accept_result = await asyncio.to_thread(run_accept, commands)
            if not accept_result["passed"]:
                return {
                    "success": False,
                    "id": task_id,
                    "acceptance_failed": accept_result["failures"],
                    "message": (
                        f"Acceptance block failed ({len(accept_result['failures'])} of "  # type: ignore[arg-type]
                        f"{accept_result['ran']} commands). Task not resolved. Fix the "
                        "underlying issue or update the accept block if the criteria are wrong."
                    ),
                }
            # On pass, prepend a marker to the resolution so the audit trail
            # records that acceptance was checked, not assumed.
            resolution = (
                f"[acceptance: {accept_result['ran']}/{accept_result['ran']} passed] " + resolution
                if resolution
                else f"[acceptance: {accept_result['ran']}/{accept_result['ran']} passed]"
            )

    resolve_result = await asyncio.to_thread(
        resolve_task,
        task_id=task_id,
        resolution=resolution,
        agent_id=ctx.agent_id,
        tenant_id=ctx.tenant_id,
    )
    out: dict[str, Any] = {"success": resolve_result, "id": task_id}
    if accept_result is not None:
        out["acceptance"] = {"passed": True, "commands_ran": accept_result["ran"]}
    return out


@_handler("list_agent_tasks")
async def _list_agent_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_agent_tasks
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        list_agent_tasks,
        agent_id=args.get("agentId", ctx.agent_id),
        include_unassigned=args.get("includeUnassigned", False),
        status=args.get("status"),
        exclude_resolved=args.get("excludeResolved", True),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope(results, _obs_scope)
        log_would_drop(
            tool_name="list_agent_tasks",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_tasks",
        )

    return {"tasks": results, "count": len(results)}


@_handler("list_my_tasks")
async def _list_my_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_agent_tasks
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        list_agent_tasks,
        agent_id=ctx.agent_id,
        include_unassigned=False,
        status=args.get("status"),
        exclude_resolved=args.get("excludeResolved", True),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope(results, _obs_scope)
        log_would_drop(
            tool_name="list_my_tasks",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_tasks",
        )

    return {"tasks": results, "count": len(results)}


# ── Task Summary Dashboard ──


@_handler("list_tasks_summary")
async def _list_tasks_summary(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Dashboard: counts by status, requires_human, by-agent, SLA overdue."""
    from psycopg2.extras import RealDictCursor

    from robothor.db.connection import get_connection

    def _query() -> dict[str, Any]:
        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # Status counts
            cur.execute(
                """SELECT status, COUNT(*) as count FROM crm_tasks
                   WHERE deleted_at IS NULL AND tenant_id = %s
                   GROUP BY status ORDER BY status""",
                (ctx.tenant_id,),
            )
            by_status = {r["status"]: r["count"] for r in cur.fetchall()}

            # Requires human count
            cur.execute(
                """SELECT COUNT(*) as count FROM crm_tasks
                   WHERE requires_human = TRUE AND resolved_at IS NULL
                     AND deleted_at IS NULL AND tenant_id = %s""",
                (ctx.tenant_id,),
            )
            requires_human = cur.fetchone()["count"]

            # By agent breakdown (top 15)
            cur.execute(
                """SELECT COALESCE(assigned_to_agent, 'unassigned') as agent,
                          status, COUNT(*) as count
                   FROM crm_tasks
                   WHERE deleted_at IS NULL AND resolved_at IS NULL AND tenant_id = %s
                   GROUP BY assigned_to_agent, status
                   ORDER BY count DESC LIMIT 30""",
                (ctx.tenant_id,),
            )
            by_agent_rows = cur.fetchall()
            by_agent: dict[str, dict[str, int]] = {}
            for r in by_agent_rows:
                agent = r["agent"]
                if agent not in by_agent:
                    by_agent[agent] = {}
                by_agent[agent][r["status"]] = r["count"]

            # SLA overdue count
            cur.execute(
                """SELECT COUNT(*) as count FROM crm_tasks
                   WHERE sla_deadline_at IS NOT NULL AND sla_deadline_at < NOW()
                     AND resolved_at IS NULL AND deleted_at IS NULL AND tenant_id = %s""",
                (ctx.tenant_id,),
            )
            sla_overdue = cur.fetchone()["count"]

            # Recent auto-task failures (tagged "failed")
            cur.execute(
                """SELECT COUNT(*) as count FROM crm_tasks
                   WHERE 'failed' = ANY(tags) AND resolved_at IS NULL
                     AND deleted_at IS NULL AND tenant_id = %s""",
                (ctx.tenant_id,),
            )
            failed_tasks = cur.fetchone()["count"]

            return {
                "by_status": by_status,
                "requires_human": requires_human,
                "by_agent": by_agent,
                "sla_overdue": sla_overdue,
                "failed_auto_tasks": failed_tasks,
            }

    return await asyncio.to_thread(_query)


# ── Task Review Workflow ──


@_handler("approve_task")
async def _approve_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import approve_task

    approve_result = await asyncio.to_thread(
        approve_task,
        task_id=args["id"],
        resolution=args.get("resolution", "Approved"),
        reviewer=ctx.agent_id or "engine",
        tenant_id=ctx.tenant_id,
    )
    if isinstance(approve_result, dict) and "error" in approve_result:
        return approve_result
    return {"success": True, "id": args["id"]}


@_handler("reject_task")
async def _reject_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import reject_task

    reject_result = await asyncio.to_thread(
        reject_task,
        task_id=args["id"],
        reason=args.get("reason", ""),
        reviewer=ctx.agent_id or "engine",
        change_requests=args.get("changeRequests"),
        tenant_id=ctx.tenant_id,
    )
    if isinstance(reject_result, dict) and "error" in reject_result:
        return reject_result
    return {"success": True, "id": args["id"]}


# ── Notifications ──


@_handler("send_notification")
async def _send_notification(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import send_notification

    nid = await asyncio.to_thread(
        send_notification,
        from_agent=args.get("fromAgent", ctx.agent_id),
        to_agent=args.get("toAgent", ""),
        notification_type=args.get("notificationType", ""),
        subject=args.get("subject", ""),
        body=args.get("body"),
        metadata=args.get("metadata"),
        task_id=args.get("taskId"),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"id": nid, "subject": args.get("subject", "")}
        if nid
        else {"error": "Failed to send notification"}
    )


@_handler("get_inbox")
async def _get_inbox(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_agent_inbox

    results = await asyncio.to_thread(
        get_agent_inbox,
        agent_id=args.get("agentId", ctx.agent_id),
        unread_only=args.get("unreadOnly", True),
        type_filter=args.get("typeFilter"),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
    )
    return {"notifications": results, "count": len(results)}


@_handler("ack_notification")
async def _ack_notification(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import acknowledge_notification

    ok = await asyncio.to_thread(
        acknowledge_notification, args.get("notificationId", ""), tenant_id=ctx.tenant_id
    )
    return {"success": ok, "id": args.get("notificationId", "")}


# ── Metadata ──


@_handler("get_metadata_objects")
async def _get_metadata_objects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_metadata_objects

    return {"objects": await asyncio.to_thread(get_metadata_objects)}


@_handler("get_object_metadata")
async def _get_object_metadata(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_object_metadata

    return await asyncio.to_thread(get_object_metadata, args.get("objectName", "")) or {
        "error": "Object not found"
    }


@_handler("search_records")
async def _search_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import search_records
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_identity_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    results = await asyncio.to_thread(
        search_records,
        query=args.get("query", ""),
        object_name=args.get("objectName"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        # search_records fans out across sub-tables with different
        # ownership shapes (crm_people is own-row-only via id; crm_notes/
        # crm_tasks are own+shared via person_id; crm_companies is
        # deliberately unscoped — see robothor.crm.dal.search_records
        # docstring) — count would-drop per table, not globally.
        by_table: dict[str, list[dict[str, Any]]] = {}
        for r in results:
            by_table.setdefault(r.get("_table", ""), []).append(r)
        for table, rows in by_table.items():
            if table == "crm_people":
                _dropped = rows_dropped_by_identity_scope(rows, _obs_scope)
            elif table in ("crm_notes", "crm_tasks"):
                _dropped = rows_dropped_by_scope(rows, _obs_scope)
            else:
                _dropped = 0  # crm_companies: unscoped by design
            log_would_drop(
                tool_name="search_records",
                user_id=ctx.user_id,
                scope=_obs_scope,
                dropped=_dropped,
                table=table,
            )

    return {"results": results, "count": len(results)}


# ── Conversations ──


@_handler("list_conversations")
async def _list_conversations(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_conversations
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    convos = await asyncio.to_thread(
        list_conversations,
        status=args.get("status", "open"),
        page=args.get("page", 1),
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope(convos, _obs_scope)
        log_would_drop(
            tool_name="list_conversations",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_conversations",
        )

    return {"conversations": convos, "count": len(convos)}


@_handler("get_conversation")
async def _get_conversation(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_conversation
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )

    _mode = data_scoping_mode()
    result = await asyncio.to_thread(
        get_conversation,
        args["conversationId"],
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_scope([result] if result else [], _obs_scope)
        log_would_drop(
            tool_name="get_conversation",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_conversations",
        )

    return result or {"error": "Conversation not found"}


@_handler("list_messages")
async def _list_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_messages
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import log_would_drop, observe_scope, scope_for_query

    # Validate at the tool boundary: LLM-hallucinated placeholder ids
    # ("cnv-00456") used to reach the integer-typed SQL parameter verbatim and
    # crash with psycopg2 InvalidTextRepresentation.
    raw_conversation_id = args.get("conversationId")
    try:
        conversation_id = int(str(raw_conversation_id))
    except (TypeError, ValueError):
        return {
            "error": (
                f"invalid conversation id {raw_conversation_id!r} — expected an integer id; "
                "use list_conversations to find real conversation ids"
            )
        }

    _mode = data_scoping_mode()
    result = await asyncio.to_thread(
        list_messages,
        conversation_id,
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )
    if isinstance(result, dict) and "error" in result:
        return result

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        # list_messages is all-or-nothing (own conversation or refused, no
        # per-row filtering) — dry-run the same ownership check enforce
        # mode would apply and log the would-be refusal, without denying.
        from robothor.crm.dal import get_conversation

        convo = await asyncio.to_thread(get_conversation, conversation_id, tenant_id=ctx.tenant_id)
        convo_person = (convo or {}).get("person_id") if convo else None
        if convo_person not in (None, _obs_scope.person_id):
            log_would_drop(
                tool_name="list_messages",
                user_id=ctx.user_id,
                scope=_obs_scope,
                dropped=len(result) if isinstance(result, list) else 0,
                table="crm_messages",
            )

    return {"payload": result}


@_handler("create_message")
async def _create_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import send_message

    msg_result = await asyncio.to_thread(
        send_message,
        conversation_id=args["conversationId"],
        content=args.get("content", ""),
        message_type=args.get("messageType", "outgoing"),
        private=args.get("private", False),
        tenant_id=ctx.tenant_id,
    )
    return dict(msg_result) if msg_result else {"error": "Failed to create message"}


@_handler("toggle_conversation_status")
async def _toggle_conversation_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import toggle_conversation_status

    ok = await asyncio.to_thread(
        toggle_conversation_status,
        conversation_id=args["conversationId"],
        status=args.get("status", "resolved"),
        tenant_id=ctx.tenant_id,
    )
    return {"success": ok, "conversationId": args["conversationId"]}


# ── Merge ──


@_handler("merge_people")
async def _merge_people(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import merge_people as _merge_people

    merge_result = await asyncio.to_thread(
        _merge_people,
        keeper_id=args.get("keeperId", ""),
        loser_id=args.get("loserId", ""),
        tenant_id=ctx.tenant_id,
    )
    if merge_result:
        return {"success": True, "keeper": merge_result}
    return {"error": "Merge failed — one or both IDs not found"}


@_handler("merge_contacts")
async def _merge_contacts(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Alias for merge_people
    return cast("dict[str, Any]", await _merge_people(args, ctx))


@_handler("merge_companies")
async def _merge_companies(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import merge_companies as _merge_companies_dal

    company_merge = await asyncio.to_thread(
        _merge_companies_dal,
        keeper_id=args.get("keeperId", ""),
        loser_id=args.get("loserId", ""),
        tenant_id=ctx.tenant_id,
    )
    if company_merge:
        return {"success": True, "keeper": company_merge}
    return {"error": "Merge failed — one or both IDs not found"}


# ── Contact 360 — agent-facing holistic lookup ───────────────────────────


@_handler("get_contact_360")
async def _get_contact_360(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the unified view of a contact: identity + counts + recent
    timeline + open tasks + recent notes + memory snippets.

    Args:
      id:              person_id UUID (preferred)
      identifier:      OR a channel identifier string (email, phone, telegram id)
      channel:         channel for identifier lookup — default 'email'
      timeline_limit:  how many timeline rows to include (default 50)
    """
    from robothor.crm.dal import (
        get_contact_360 as _dal_get_contact_360,
    )
    from robothor.crm.dal import (
        resolve_contact,
    )

    person_id = (args.get("id") or "").strip()
    if not person_id:
        identifier = (args.get("identifier") or "").strip()
        if not identifier:
            return {"error": "id or identifier is required"}
        channel = (args.get("channel") or "email").strip()
        mapping = await asyncio.to_thread(resolve_contact, channel, identifier, None, ctx.tenant_id)
        person_id = str(mapping.get("person_id") or "").strip()
        if not person_id:
            return {"error": f"no CRM person mapped to {channel}:{identifier}"}

    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_identity_scope,
        scope_for_query,
    )

    # get_contact_360 is a "give me everything about person X" call, not a
    # filtered listing — own-row-only, no org-general carve-out (see
    # robothor.crm.dal.get_contact_360): a restricted mismatch is a hard
    # denial in enforce, "the whole record" in observe/off.
    _mode = data_scoping_mode()
    timeline_limit = int(args.get("timeline_limit", 50))
    result = await asyncio.to_thread(
        _dal_get_contact_360,
        person_id,
        tenant_id=ctx.tenant_id,
        timeline_limit=timeline_limit,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_identity_scope([{"id": person_id}], _obs_scope)
        log_would_drop(
            tool_name="get_contact_360",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="crm_people",
        )

    return result


@_handler("list_contact_messages")
async def _list_contact_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Fetch message bodies for a person, optionally filtered by channel."""
    from robothor.crm.dal import get_person_messages
    from robothor.engine.feature_flags import data_scoping_mode
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_identity_scope,
        scope_for_query,
    )

    person_id = (args.get("id") or "").strip()
    if not person_id:
        return {"error": "id is required"}
    channel = args.get("channel")
    limit = int(args.get("limit", 100))

    # get_person_messages is a "give me this person's messages" call — a
    # person's messages are inherently person-linked, unlike list_messages's
    # conversation which can be unlinked. Own-row-only, no org-general
    # carve-out (see robothor.crm.dal.get_person_messages): a restricted
    # mismatch is a hard denial in enforce, the real messages in observe/off.
    _mode = data_scoping_mode()
    rows = await asyncio.to_thread(
        get_person_messages,
        person_id,
        channel=channel,
        limit=limit,
        tenant_id=ctx.tenant_id,
        scope=scope_for_query(_mode, ctx.identity),
    )

    _obs_scope = observe_scope(_mode, ctx.identity)
    if _obs_scope:
        _dropped = rows_dropped_by_identity_scope([{"id": person_id}], _obs_scope)
        log_would_drop(
            tool_name="list_contact_messages",
            user_id=ctx.user_id,
            scope=_obs_scope,
            dropped=_dropped,
            table="message",
        )

    if isinstance(rows, dict) and "error" in rows:
        return rows
    return {"messages": rows}
