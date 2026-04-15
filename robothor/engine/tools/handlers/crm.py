"""CRM tool handlers — people, companies, notes, tasks, conversations, merge, metadata, notifications."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
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

    return await asyncio.to_thread(get_person, args["id"], tenant_id=ctx.tenant_id) or {
        "error": "Person not found"
    }


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
    }
    kwargs = {dal_key: args[k] for k, dal_key in field_map.items() if k in args and k != "id"}
    ok = await asyncio.to_thread(update_person, pid, tenant_id=ctx.tenant_id, **kwargs)
    return {"success": ok, "id": pid}


@_handler("list_people")
async def _list_people(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_people

    results = await asyncio.to_thread(
        list_people,
        search=args.get("search"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
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

    return await asyncio.to_thread(get_note, args["id"], tenant_id=ctx.tenant_id) or {
        "error": "Note not found"
    }


@_handler("list_notes")
async def _list_notes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_notes

    results = await asyncio.to_thread(
        list_notes,
        person_id=args.get("personId"),
        company_id=args.get("companyId"),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
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

    task_id = await asyncio.to_thread(
        create_task,
        title=args.get("title", ""),
        body=args.get("body"),
        status=args.get("status", "TODO"),
        due_at=args.get("dueAt"),
        person_id=args.get("personId"),
        company_id=args.get("companyId"),
        assigned_to_agent=args.get("assignedToAgent"),
        created_by_agent=args.get("createdByAgent", ctx.agent_id),
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

    return await asyncio.to_thread(get_task, args["id"], tenant_id=ctx.tenant_id) or {
        "error": "Task not found"
    }


@_handler("list_tasks")
async def _list_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_tasks

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
    ok = await asyncio.to_thread(update_task, tid, tenant_id=ctx.tenant_id, **kwargs)
    return {"success": ok, "id": tid}


@_handler("delete_task")
async def _delete_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import delete_task

    ok = await asyncio.to_thread(delete_task, args["id"], tenant_id=ctx.tenant_id)
    return {"success": ok, "id": args["id"]}


@_handler("resolve_task")
async def _resolve_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import resolve_task

    resolve_result = await asyncio.to_thread(
        resolve_task,
        task_id=args["id"],
        resolution=args.get("resolution", ""),
        agent_id=ctx.agent_id,
        tenant_id=ctx.tenant_id,
    )
    return {"success": resolve_result, "id": args["id"]}


@_handler("list_agent_tasks")
async def _list_agent_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_agent_tasks

    results = await asyncio.to_thread(
        list_agent_tasks,
        agent_id=args.get("agentId", ctx.agent_id),
        include_unassigned=args.get("includeUnassigned", False),
        status=args.get("status"),
        exclude_resolved=args.get("excludeResolved", True),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
    )
    return {"tasks": results, "count": len(results)}


@_handler("list_my_tasks")
async def _list_my_tasks(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_agent_tasks

    results = await asyncio.to_thread(
        list_agent_tasks,
        agent_id=ctx.agent_id,
        include_unassigned=False,
        status=args.get("status"),
        exclude_resolved=args.get("excludeResolved", True),
        limit=args.get("limit", 50),
        tenant_id=ctx.tenant_id,
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

    results = await asyncio.to_thread(
        search_records,
        query=args.get("query", ""),
        object_name=args.get("objectName"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
    )
    return {"results": results, "count": len(results)}


# ── Conversations ──


@_handler("list_conversations")
async def _list_conversations(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_conversations

    convos = await asyncio.to_thread(
        list_conversations,
        status=args.get("status", "open"),
        page=args.get("page", 1),
        tenant_id=ctx.tenant_id,
    )
    return {"conversations": convos, "count": len(convos)}


@_handler("get_conversation")
async def _get_conversation(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import get_conversation

    return await asyncio.to_thread(
        get_conversation, args["conversationId"], tenant_id=ctx.tenant_id
    ) or {"error": "Conversation not found"}


@_handler("list_messages")
async def _list_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import list_messages

    return {
        "payload": await asyncio.to_thread(
            list_messages, args["conversationId"], tenant_id=ctx.tenant_id
        )
    }


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


# ── Agent Reviews ──


@_handler("review_agent")
async def _review_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Submit a review for an agent."""
    from robothor.crm.dal import create_review

    agent_id = args.get("agent_id", "").strip()
    if not agent_id:
        return {"error": "agent_id is required"}
    rating = args.get("rating")
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        return {"error": "rating must be an integer 1-5"}

    review_id = await asyncio.to_thread(
        create_review,
        agent_id=agent_id,
        reviewer=args.get("reviewer", ctx.agent_id),
        reviewer_type=args.get("reviewer_type", "agent"),
        rating=rating,
        categories=args.get("categories"),
        feedback=args.get("feedback"),
        action_items=args.get("action_items"),
        run_id=args.get("run_id"),
        tenant_id=ctx.tenant_id,
    )
    return (
        {"success": True, "review_id": review_id}
        if review_id
        else {"error": "Failed to create review"}
    )


@_handler("get_agent_reviews")
async def _get_agent_reviews(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get reviews and summary for an agent."""
    from robothor.crm.dal import get_review_summary, get_reviews

    agent_id = args.get("agent_id", "").strip()
    if not agent_id:
        return {"error": "agent_id is required"}

    days = args.get("days", 30)
    reviews = await asyncio.to_thread(get_reviews, agent_id, days=days, tenant_id=ctx.tenant_id)
    summary = await asyncio.to_thread(
        get_review_summary, agent_id, days=days, tenant_id=ctx.tenant_id
    )
    return {"reviews": reviews, "summary": summary}
