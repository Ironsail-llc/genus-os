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


def person_to_dict(row: dict) -> dict:
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
        } if row.get("company_id") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def company_to_dict(row: dict) -> dict:
    """Convert a crm_companies row to API response shape."""
    return {
        "id": str(row["id"]),
        "name": row.get("name") or "",
        "domainName": row.get("domain_name") or "",
        "employees": row.get("employees"),
        "address": row.get("address") or "",
        "linkedinUrl": row.get("linkedin_url") or "",
        "idealCustomerProfile": row.get("ideal_customer_profile", False),
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def note_to_dict(row: dict) -> dict:
    """Convert a crm_notes row to API response shape."""
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def task_to_dict(row: dict) -> dict:
    """Convert a crm_tasks row to API response shape."""
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "status": row.get("status") or "TODO",
        "dueAt": row["due_at"].isoformat() if row.get("due_at") else None,
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def conversation_to_dict(row: dict) -> dict:
    """Convert a crm_conversations row to API response shape."""
    return {
        "id": row["id"],  # Integer, not UUID
        "status": row.get("status") or "open",
        "inboxName": row.get("inbox_name") or "",
        "messagesCount": row.get("messages_count") or 0,
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "personName": row.get("person_name") or row.get("display_name") or "",
        "metadata": row.get("metadata") or {},
        "lastActivityAt": row["last_activity_at"].isoformat() if row.get("last_activity_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }
