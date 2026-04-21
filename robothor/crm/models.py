"""
CRM Response Models — shape converters for database rows.

Converts raw PostgreSQL row dicts into standardized response shapes.
These are used by the Bridge API, MCP server, and pipeline ingestion.

Usage:
    from robothor.crm.models import person_to_dict, company_to_dict

    row = cursor.fetchone()  # RealDictCursor row
    response = person_to_dict(row)
"""

from __future__ import annotations

from typing import Any

from robothor.constants import DEFAULT_TENANT


def person_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_people row to API response shape.

    Response format maintains backward compatibility with Twenty CRM GraphQL shape
    (name object, emails object, phones object).
    """
    return {
        "id": str(row["id"]),
        "name": {
            "firstName": row.get("first_name") or "",
            "lastName": row.get("last_name") or "",
        },
        "emails": {
            "primaryEmail": row.get("email") or "",
        },
        "phones": {
            "primaryPhoneNumber": row.get("phone") or "",
        },
        "jobTitle": row.get("job_title") or "",
        "city": row.get("city") or "",
        "avatarUrl": row.get("avatar_url") or "",
        "linkedinUrl": row.get("linkedin_url") or "",
        "additionalEmails": row.get("additional_emails") or [],
        "additionalPhones": row.get("additional_phones") or [],
        "company": {
            "id": str(row["company_id"]) if row.get("company_id") else None,
            "name": row.get("company_name") or "",
        }
        if row.get("company_id")
        else None,
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "schedulingPolicy": row.get("scheduling_policy") or "stable",
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def company_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_companies row to API response shape."""
    return {
        "id": str(row["id"]),
        "name": row.get("name") or "",
        "domainName": row.get("domain_name") or "",
        "employees": row.get("employees"),
        "address": row.get("address") or "",
        "linkedinUrl": row.get("linkedin_url") or "",
        "idealCustomerProfile": row.get("ideal_customer_profile", False),
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def note_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_notes row to API response shape."""
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def task_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_tasks row to API response shape."""
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "status": row.get("status") or "TODO",
        "dueAt": row["due_at"].isoformat() if row.get("due_at") else None,
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "createdByAgent": row.get("created_by_agent") or "",
        "assignedToAgent": row.get("assigned_to_agent") or "",
        "priority": row.get("priority") or "normal",
        "tags": row.get("tags") or [],
        "parentTaskId": str(row["parent_task_id"]) if row.get("parent_task_id") else None,
        "resolvedAt": row["resolved_at"].isoformat() if row.get("resolved_at") else None,
        "resolution": row.get("resolution") or "",
        "slaDeadlineAt": row["sla_deadline_at"].isoformat() if row.get("sla_deadline_at") else None,
        "escalationCount": row.get("escalation_count") or 0,
        "startedAt": row["started_at"].isoformat() if row.get("started_at") else None,
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "requiresHuman": bool(row.get("requires_human", False)),
        "objective": row.get("objective"),
        "nextAction": row.get("next_action"),
        "nextActionAgent": row.get("next_action_agent"),
        "blockers": row.get("blockers") or [],
        "questionForOperator": row.get("question_for_operator"),
        "autonomyBudget": row.get("autonomy_budget") or {},
        "lastPlannedAt": row["last_planned_at"].isoformat() if row.get("last_planned_at") else None,
        "plannerVersion": row.get("planner_version") or 0,
    }


def history_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_task_history row to API response shape."""
    return {
        "id": str(row["id"]),
        "taskId": str(row["task_id"]),
        "fromStatus": row.get("from_status"),
        "toStatus": row.get("to_status") or "",
        "changedBy": row.get("changed_by") or "",
        "reason": row.get("reason") or "",
        "metadata": row.get("metadata") or {},
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def routine_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_routines row to API response shape."""
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "cronExpr": row.get("cron_expr") or "",
        "timezone": row.get("timezone") or "America/New_York",
        "assignedToAgent": row.get("assigned_to_agent") or "",
        "priority": row.get("priority") or "normal",
        "tags": row.get("tags") or [],
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "active": row.get("active", True),
        "nextRunAt": row["next_run_at"].isoformat() if row.get("next_run_at") else None,
        "lastRunAt": row["last_run_at"].isoformat() if row.get("last_run_at") else None,
        "createdBy": row.get("created_by") or "",
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def conversation_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_conversations row to API response shape."""
    return {
        "id": row["id"],  # Integer, not UUID
        "status": row.get("status") or "open",
        "inboxName": row.get("inbox_name") or "",
        "messagesCount": row.get("messages_count") or 0,
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "personName": row.get("person_name") or row.get("display_name") or "",
        "metadata": row.get("metadata") or {},
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "lastActivityAt": row["last_activity_at"].isoformat()
        if row.get("last_activity_at")
        else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def tenant_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_tenants row to API response shape."""
    return {
        "id": row["id"],
        "displayName": row.get("display_name") or "",
        "parentTenantId": row.get("parent_tenant_id"),
        "settings": row.get("settings") or {},
        "active": row.get("active", True),
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def notification_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a crm_agent_notifications row to API response shape."""
    return {
        "id": str(row["id"]),
        "tenantId": row.get("tenant_id") or DEFAULT_TENANT,
        "fromAgent": row.get("from_agent") or "",
        "toAgent": row.get("to_agent") or "",
        "notificationType": row.get("notification_type") or "",
        "subject": row.get("subject") or "",
        "body": row.get("body") or "",
        "metadata": row.get("metadata") or {},
        "taskId": str(row["task_id"]) if row.get("task_id") else None,
        "readAt": row["read_at"].isoformat() if row.get("read_at") else None,
        "acknowledgedAt": row["acknowledged_at"].isoformat()
        if row.get("acknowledged_at")
        else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }
