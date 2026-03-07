"""
CRM Data Access Layer — PostgreSQL CRUD for all CRM entities.

All operations use soft deletes (deleted_at). All mutations are audit-logged.
Response shapes are defined in robothor.crm.models.

Multi-tenant: every function accepts ``tenant_id`` (default ``"robothor-primary"``).

Usage:
    from robothor.crm.dal import create_person, search_people, get_person

    person_id = create_person("Jane", "Smith", email="jane@example.com")
    people = search_people("Jane")
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.crm.models import (
    company_to_dict,
    conversation_to_dict,
    history_to_dict,
    note_to_dict,
    notification_to_dict,
    person_to_dict,
    routine_to_dict,
    task_to_dict,
    tenant_to_dict,
)
from robothor.crm.validation import (
    COMPANY_BLOCKLIST,
    normalize_email,
    scrub_null_string,
    validate_person_input,
)
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "robothor-primary"


def _safe_audit(operation: str, entity_type: str, entity_id: str | None, **kwargs: Any) -> None:
    """Wrap audit logging so it never propagates exceptions."""
    try:
        from robothor.audit.logger import log_crm_mutation

        log_crm_mutation(operation, entity_type, entity_id, **kwargs)
    except Exception as e:
        logger.warning("Audit call failed (non-fatal): %s", e)


# ─── People ──────────────────────────────────────────────────────────────


def search_people(name: str, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
    """Search people by name (ILIKE on first_name/last_name)."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        pattern = f"%{name}%"
        cur.execute(
            """
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.deleted_at IS NULL AND p.tenant_id = %s
              AND (p.first_name ILIKE %s OR p.last_name ILIKE %s)
            ORDER BY p.updated_at DESC
            LIMIT 50
        """,
            (tenant_id, pattern, pattern),
        )
        return [person_to_dict(r) for r in cur.fetchall()]


def create_person(
    first_name: str,
    last_name: str = "",
    email: str | None = None,
    phone: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a person. Returns person UUID, or None if blocked/invalid."""
    valid, reason = validate_person_input(first_name, last_name, email)
    if not valid:
        logger.info("Blocked create_person(%s %s): %s", first_name, last_name, reason)
        return None

    email = normalize_email(email)
    first_name = scrub_null_string(first_name) or first_name
    last_name = scrub_null_string(last_name) or last_name

    person_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO crm_people (id, first_name, last_name, email, phone, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (person_id, first_name, last_name, email, phone, tenant_id),
            )
            conn.commit()
            _safe_audit(
                "create",
                "person",
                person_id,
                details={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "tenant_id": tenant_id,
                },
            )
            return person_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create person: %s", e)
            return None


def get_person(person_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any] | None:
    """Get a person by ID, with company JOIN."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.id = %s AND p.deleted_at IS NULL AND p.tenant_id = %s
        """,
            (person_id, tenant_id),
        )
        row = cur.fetchone()
        return person_to_dict(row) if row else None


def update_person(person_id: str, tenant_id: str = DEFAULT_TENANT, **fields: Any) -> bool:
    """Update a person's fields. Only sets non-None fields."""
    for key in ("job_title", "city", "first_name", "last_name"):
        if key in fields and fields[key] is not None:
            fields[key] = scrub_null_string(fields[key])

    col_map = {
        "job_title": "job_title",
        "company_id": "company_id",
        "city": "city",
        "linkedin_url": "linkedin_url",
        "phone": "phone",
        "avatar_url": "avatar_url",
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])

    for jsonb_field in ("additional_emails", "additional_phones"):
        if jsonb_field in fields and fields[jsonb_field] is not None:
            sets.append(f"{jsonb_field} = %s::jsonb")
            vals.append(json.dumps(fields[jsonb_field]))

    if not sets:
        return False

    sets.append("updated_at = NOW()")
    vals.extend([person_id, tenant_id])

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_people SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("update", "person", person_id, details={"fields": list(fields.keys())})
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update person %s: %s", person_id, e)
            return False


def delete_person(person_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Soft-delete a person."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_people SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (person_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("delete", "person", person_id)
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to delete person %s: %s", person_id, e)
            return False


def merge_people(
    keeper_id: str, loser_id: str, tenant_id: str = DEFAULT_TENANT
) -> dict[str, Any] | None:
    """Merge loser into keeper in a single transaction.

    1. Fill keeper's empty fields from loser
    2. Collect loser's emails/phones into keeper's additional_emails/phones
    3. Re-point contact_identifiers, conversations, notes, tasks
    4. Soft-delete loser
    5. Create merge note on keeper
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (keeper_id, tenant_id),
            )
            keeper = cur.fetchone()
            cur.execute(
                "SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (loser_id, tenant_id),
            )
            loser = cur.fetchone()

            if not keeper or not loser:
                logger.error("merge_people: keeper or loser not found")
                return None

            # 1. Fill empty fields
            fillable = [
                "first_name",
                "last_name",
                "email",
                "phone",
                "job_title",
                "city",
                "linkedin_url",
                "avatar_url",
                "company_id",
            ]
            updates: list[str] = []
            update_vals: list[Any] = []
            for field in fillable:
                keeper_val = keeper.get(field)
                loser_val = loser.get(field)
                keeper_empty = not keeper_val or (
                    isinstance(keeper_val, str) and not keeper_val.strip()
                )
                loser_has = loser_val and (not isinstance(loser_val, str) or loser_val.strip())
                if keeper_empty and loser_has:
                    updates.append(f"{field} = %s")
                    update_vals.append(loser_val)

            # 2. Collect emails/phones
            existing_emails: list[str] = keeper.get("additional_emails") or []
            existing_phones: list[str] = keeper.get("additional_phones") or []

            loser_email = loser.get("email")
            keeper_email = keeper.get("email")
            if loser_email and loser_email != keeper_email and loser_email not in existing_emails:
                existing_emails.append(loser_email)
            for e in loser.get("additional_emails") or []:
                if e and e != keeper_email and e not in existing_emails:
                    existing_emails.append(e)

            loser_phone = loser.get("phone")
            keeper_phone = keeper.get("phone")
            if loser_phone and loser_phone != keeper_phone and loser_phone not in existing_phones:
                existing_phones.append(loser_phone)
            for p in loser.get("additional_phones") or []:
                if p and p != keeper_phone and p not in existing_phones:
                    existing_phones.append(p)

            if existing_emails:
                updates.append("additional_emails = %s::jsonb")
                update_vals.append(json.dumps(existing_emails))
            if existing_phones:
                updates.append("additional_phones = %s::jsonb")
                update_vals.append(json.dumps(existing_phones))

            if updates:
                updates.append("updated_at = NOW()")
                update_vals.append(keeper_id)
                cur.execute(
                    f"UPDATE crm_people SET {', '.join(updates)} WHERE id = %s",
                    update_vals,
                )

            # 3. Re-point related records
            cur.execute(
                "UPDATE contact_identifiers SET person_id = %s WHERE person_id = %s",
                (keeper_id, loser_id),
            )
            cur.execute(
                "UPDATE crm_conversations SET person_id = %s WHERE person_id = %s",
                (keeper_id, loser_id),
            )
            cur.execute(
                "UPDATE crm_notes SET person_id = %s WHERE person_id = %s AND deleted_at IS NULL",
                (keeper_id, loser_id),
            )
            cur.execute(
                "UPDATE crm_tasks SET person_id = %s WHERE person_id = %s AND deleted_at IS NULL",
                (keeper_id, loser_id),
            )

            # 4. Soft-delete loser
            cur.execute(
                "UPDATE crm_people SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s",
                (loser_id,),
            )

            # 5. Create merge note
            loser_name = f"{loser.get('first_name', '')} {loser.get('last_name', '')}".strip()
            note_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO crm_notes (id, title, body, person_id, tenant_id) VALUES (%s, %s, %s, %s, %s)",
                (
                    note_id,
                    "Duplicate Merged",
                    f"Merged duplicate: {loser_name} (id: {loser_id})",
                    keeper_id,
                    tenant_id,
                ),
            )

            conn.commit()
            _safe_audit(
                "merge",
                "person",
                keeper_id,
                details={"loser_id": loser_id, "loser_name": loser_name},
            )
            return get_person(keeper_id, tenant_id)

        except Exception as e:
            conn.rollback()
            logger.error("Failed to merge person %s into %s: %s", loser_id, keeper_id, e)
            return None


def merge_companies(
    keeper_id: str, loser_id: str, tenant_id: str = DEFAULT_TENANT
) -> dict[str, Any] | None:
    """Merge loser company into keeper.

    1. Fill keeper's empty fields from loser
    2. Re-point crm_people.company_id, crm_notes.company_id
    3. Soft-delete loser
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (keeper_id, tenant_id),
            )
            keeper = cur.fetchone()
            cur.execute(
                "SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (loser_id, tenant_id),
            )
            loser = cur.fetchone()

            if not keeper or not loser:
                logger.error("merge_companies: keeper or loser not found")
                return None

            fillable = ["domain_name", "employees", "address", "linkedin_url"]
            updates: list[str] = []
            update_vals: list[Any] = []
            for field in fillable:
                keeper_val = keeper.get(field)
                loser_val = loser.get(field)
                keeper_empty = not keeper_val or (
                    isinstance(keeper_val, str) and not keeper_val.strip()
                )
                loser_has = loser_val and (not isinstance(loser_val, str) or loser_val.strip())
                if keeper_empty and loser_has:
                    updates.append(f"{field} = %s")
                    update_vals.append(loser_val)

            if updates:
                updates.append("updated_at = NOW()")
                update_vals.append(keeper_id)
                cur.execute(
                    f"UPDATE crm_companies SET {', '.join(updates)} WHERE id = %s",
                    update_vals,
                )

            cur.execute(
                "UPDATE crm_people SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
                (keeper_id, loser_id),
            )
            cur.execute(
                "UPDATE crm_notes SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
                (keeper_id, loser_id),
            )
            cur.execute(
                "UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s",
                (loser_id,),
            )

            conn.commit()
            _safe_audit(
                "merge",
                "company",
                keeper_id,
                details={"loser_id": loser_id, "loser_name": loser.get("name", "")},
            )
            return get_company(keeper_id, tenant_id)

        except Exception as e:
            conn.rollback()
            logger.error("Failed to merge company %s into %s: %s", loser_id, keeper_id, e)
            return None


def list_people(
    search: str | None = None, limit: int = 20, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """List people, optionally filtered by search term."""
    if search:
        return search_people(search, tenant_id)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.deleted_at IS NULL AND p.tenant_id = %s
            ORDER BY p.updated_at DESC LIMIT %s
        """,
            (tenant_id, limit),
        )
        return [person_to_dict(r) for r in cur.fetchall()]


# ─── Companies ───────────────────────────────────────────────────────────


def find_or_create_company(name: str, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Find a company by name (ILIKE), or create it. Returns company UUID."""
    if name.strip().lower() in COMPANY_BLOCKLIST:
        logger.info("Blocked find_or_create_company(%s): in blocklist", name)
        return None

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id FROM crm_companies WHERE name ILIKE %s AND deleted_at IS NULL AND tenant_id = %s LIMIT 1",
            (f"%{name}%", tenant_id),
        )
        row = cur.fetchone()
        if row:
            result: str = str(row["id"])
            return result

        company_id = str(uuid.uuid4())
        try:
            cur.execute(
                "INSERT INTO crm_companies (id, name, tenant_id) VALUES (%s, %s, %s)",
                (company_id, name, tenant_id),
            )
            conn.commit()
            _safe_audit(
                "create", "company", company_id, details={"name": name, "via": "find_or_create"}
            )
            return company_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create company: %s", e)
            return None


def create_company(
    name: str,
    domain_name: str | None = None,
    employees: int | None = None,
    address: str | None = None,
    linkedin_url: str | None = None,
    ideal_customer_profile: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a company. Returns company UUID."""
    company_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO crm_companies (id, name, domain_name, employees,
                    address, linkedin_url, ideal_customer_profile, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    company_id,
                    name,
                    domain_name,
                    employees,
                    address,
                    linkedin_url,
                    ideal_customer_profile,
                    tenant_id,
                ),
            )
            conn.commit()
            _safe_audit("create", "company", company_id, details={"name": name})
            return company_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create company: %s", e)
            return None


def get_company(company_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any] | None:
    """Get a company by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
            (company_id, tenant_id),
        )
        row = cur.fetchone()
        return company_to_dict(row) if row else None


def update_company(company_id: str, tenant_id: str = DEFAULT_TENANT, **fields: Any) -> bool:
    """Update a company's fields. Only sets non-None fields."""
    col_map = {
        "domain_name": "domain_name",
        "employees": "employees",
        "address": "address",
        "linkedin_url": "linkedin_url",
        "ideal_customer_profile": "ideal_customer_profile",
        "name": "name",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])

    if not sets:
        return False

    sets.append("updated_at = NOW()")
    vals.extend([company_id, tenant_id])

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_companies SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit(
                    "update", "company", company_id, details={"fields": list(fields.keys())}
                )
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update company %s: %s", company_id, e)
            return False


def delete_company(company_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Soft-delete a company."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (company_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("delete", "company", company_id)
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to delete company %s: %s", company_id, e)
            return False


def list_companies(
    search: str | None = None, limit: int = 50, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """List companies, optionally filtered by name search."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if search:
            cur.execute(
                """
                SELECT * FROM crm_companies
                WHERE deleted_at IS NULL AND tenant_id = %s AND name ILIKE %s
                ORDER BY updated_at DESC LIMIT %s
            """,
                (tenant_id, f"%{search}%", limit),
            )
        else:
            cur.execute(
                """
                SELECT * FROM crm_companies
                WHERE deleted_at IS NULL AND tenant_id = %s
                ORDER BY updated_at DESC LIMIT %s
            """,
                (tenant_id, limit),
            )
        return [company_to_dict(r) for r in cur.fetchall()]


# ─── Notes ───────────────────────────────────────────────────────────────


def create_note(
    title: str,
    body: str,
    person_id: str | None = None,
    company_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a note. Returns note UUID."""
    note_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO crm_notes (id, title, body, person_id, company_id, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (note_id, title, body, person_id, company_id, tenant_id),
            )
            conn.commit()
            _safe_audit("create", "note", note_id, details={"title": title})
            return note_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create note: %s", e)
            return None


def get_note(note_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any] | None:
    """Get a note by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_notes WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
            (note_id, tenant_id),
        )
        row = cur.fetchone()
        return note_to_dict(row) if row else None


def list_notes(
    person_id: str | None = None,
    company_id: str | None = None,
    limit: int = 50,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """List notes with optional person/company filter."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL", "tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if person_id:
            conditions.append("person_id = %s")
            params.append(person_id)
        if company_id:
            conditions.append("company_id = %s")
            params.append(company_id)
        params.append(limit)
        cur.execute(
            f"SELECT * FROM crm_notes WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT %s",
            params,
        )
        return [note_to_dict(r) for r in cur.fetchall()]


def update_note(note_id: str, tenant_id: str = DEFAULT_TENANT, **fields: Any) -> bool:
    """Update a note. Accepted: title, body, person_id, company_id."""
    col_map = {
        "title": "title",
        "body": "body",
        "person_id": "person_id",
        "company_id": "company_id",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.extend([note_id, tenant_id])
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_notes SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("update", "note", note_id, details={"fields": list(fields.keys())})
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update note %s: %s", note_id, e)
            return False


def delete_note(note_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Soft-delete a note."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_notes SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (note_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("delete", "note", note_id)
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to delete note %s: %s", note_id, e)
            return False


# ─── Tasks ───────────────────────────────────────────────────────────────

# Valid status transitions (from -> set of allowed targets)
VALID_TRANSITIONS: dict[str, set[str]] = {
    "TODO": {"IN_PROGRESS", "DONE"},
    "IN_PROGRESS": {"REVIEW", "TODO", "DONE"},
    "REVIEW": {"DONE", "IN_PROGRESS", "TODO"},
    "DONE": {"TODO"},
}

# SLA deadlines by priority
SLA_DEADLINES: dict[str, timedelta] = {
    "urgent": timedelta(minutes=30),
    "high": timedelta(hours=2),
    "normal": timedelta(hours=8),
    "low": timedelta(hours=24),
}


def _validate_transition(
    current: str, target: str, resolution: str | None = None
) -> tuple[bool, str]:
    """Validate a status transition. Returns (ok, reason)."""
    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None:
        return False, f"Unknown current status '{current}'"
    if target not in allowed:
        return (
            False,
            f"Cannot transition from {current} to {target}. Allowed: {', '.join(sorted(allowed))}",
        )
    if target == "DONE" and current == "REVIEW" and not resolution:
        return False, "REVIEW -> DONE requires a resolution"
    return True, ""


def _compute_sla_deadline(priority: str) -> datetime | None:
    """Compute SLA deadline from priority."""
    delta = SLA_DEADLINES.get(priority)
    if delta is None:
        return None
    return datetime.now(UTC) + delta


def _record_transition(
    cur: Any,
    task_id: str,
    from_status: str | None,
    to_status: str,
    changed_by: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Append a row to crm_task_history."""
    cur.execute(
        """INSERT INTO crm_task_history (id, task_id, from_status, to_status, changed_by, reason, metadata, tenant_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            str(uuid.uuid4()),
            task_id,
            from_status,
            to_status,
            changed_by,
            reason,
            json.dumps(metadata) if metadata else None,
            tenant_id,
        ),
    )


def _check_subtask_completion(cur: Any, task_id: str) -> str | None:
    """Check if child tasks are incomplete. Returns warning message or None."""
    cur.execute(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE status != 'DONE') AS incomplete
           FROM crm_tasks
           WHERE parent_task_id = %s AND deleted_at IS NULL""",
        (task_id,),
    )
    row = cur.fetchone()
    if row and row["total"] > 0 and row["incomplete"] > 0:
        return f"{row['incomplete']} of {row['total']} subtasks are not DONE"
    return None


def find_task_by_thread_id(
    thread_id: str,
    assigned_to_agent: str | None = None,
    include_recently_resolved: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Find a task containing a threadId in its body.

    Used for server-side dedup — prevents duplicate tasks for the same email thread.
    When include_recently_resolved=True, also matches tasks resolved within the last
    72 hours (prevents re-creation of tasks Philip already resolved).
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if include_recently_resolved:
            resolved_clause = "(resolved_at IS NULL OR resolved_at > NOW() - INTERVAL '72 hours')"
        else:
            resolved_clause = "resolved_at IS NULL"
        query = f"""
            SELECT id, title, status FROM crm_tasks
            WHERE body LIKE %s
              AND {resolved_clause}
              AND deleted_at IS NULL
              AND tenant_id = %s
        """
        params: list[Any] = [f"%threadId: {thread_id}%", tenant_id]
        if assigned_to_agent:
            query += " AND assigned_to_agent = %s"
            params.append(assigned_to_agent)
        query += " LIMIT 1"
        cur.execute(query, params)
        row = cur.fetchone()
        if row:
            return dict(row)
        return None


def create_task(
    title: str,
    body: str | None = None,
    status: str = "TODO",
    due_at: str | None = None,
    person_id: str | None = None,
    company_id: str | None = None,
    created_by_agent: str | None = None,
    assigned_to_agent: str | None = None,
    priority: str = "normal",
    tags: list[str] | None = None,
    parent_task_id: str | None = None,
    requires_human: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a task. Returns task UUID."""
    task_id = str(uuid.uuid4())
    sla_deadline = _compute_sla_deadline(priority)
    started = datetime.now(UTC) if status == "IN_PROGRESS" else None
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                """
                INSERT INTO crm_tasks (id, title, body, status, due_at, person_id, company_id,
                                       created_by_agent, assigned_to_agent, priority, tags,
                                       parent_task_id, sla_deadline_at, started_at, requires_human,
                                       tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    task_id,
                    title,
                    body,
                    status,
                    due_at,
                    person_id,
                    company_id,
                    created_by_agent,
                    assigned_to_agent,
                    priority,
                    tags or [],
                    parent_task_id,
                    sla_deadline,
                    started,
                    requires_human,
                    tenant_id,
                ),
            )
            _record_transition(
                cur,
                task_id,
                None,
                status,
                changed_by=created_by_agent or "system",
                reason="Task created",
                tenant_id=tenant_id,
            )
            conn.commit()
            _safe_audit(
                "create",
                "task",
                task_id,
                details={
                    "title": title,
                    "assigned_to_agent": assigned_to_agent,
                    "created_by_agent": created_by_agent,
                    "priority": priority,
                },
            )
            return task_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create task: %s", e)
            return None


def get_task(task_id: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any] | None:
    """Get a task by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_tasks WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
            (task_id, tenant_id),
        )
        row = cur.fetchone()
        return task_to_dict(row) if row else None


def list_tasks(
    status: str | None = None,
    person_id: str | None = None,
    limit: int = 50,
    assigned_to_agent: str | None = None,
    created_by_agent: str | None = None,
    tags: list[str] | None = None,
    priority: str | None = None,
    parent_task_id: str | None = None,
    exclude_resolved: bool = True,
    requires_human: bool | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """List tasks with optional filters."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL", "tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("status = %s")
            params.append(status)
        if person_id:
            conditions.append("person_id = %s")
            params.append(person_id)
        if assigned_to_agent:
            conditions.append("assigned_to_agent = %s")
            params.append(assigned_to_agent)
        if created_by_agent:
            conditions.append("created_by_agent = %s")
            params.append(created_by_agent)
        if tags:
            conditions.append("tags @> %s")
            params.append(tags)
        if priority:
            conditions.append("priority = %s")
            params.append(priority)
        if parent_task_id:
            conditions.append("parent_task_id = %s")
            params.append(parent_task_id)
        if exclude_resolved and not status:
            conditions.append("status != 'DONE'")
        if requires_human is not None:
            conditions.append("requires_human = %s")
            params.append(requires_human)
        params.append(limit)
        cur.execute(
            f"SELECT * FROM crm_tasks WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT %s",
            params,
        )
        return [task_to_dict(r) for r in cur.fetchall()]


def update_task(
    task_id: str, changed_by: str | None = None, tenant_id: str = DEFAULT_TENANT, **fields: Any
) -> bool | dict[str, Any]:
    """Update a task.

    Accepted: title, body, status, due_at, person_id, company_id,
              created_by_agent, assigned_to_agent, priority, tags,
              parent_task_id, resolved_at, resolution, requires_human.

    When a status transition is requested:
    - Validates against VALID_TRANSITIONS
    - Records history in crm_task_history
    - Returns dict with warning if subtasks are incomplete
    - Returns ``{"error": reason, "from": ..., "to": ...}`` on invalid transition

    Returns True on success, dict on transition with details, False if not found.
    """
    new_status = fields.get("status")

    col_map = {
        "title": "title",
        "body": "body",
        "status": "status",
        "due_at": "due_at",
        "person_id": "person_id",
        "company_id": "company_id",
        "created_by_agent": "created_by_agent",
        "assigned_to_agent": "assigned_to_agent",
        "priority": "priority",
        "parent_task_id": "parent_task_id",
        "resolved_at": "resolved_at",
        "resolution": "resolution",
        "requires_human": "requires_human",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    # tags needs special handling (array type)
    if "tags" in fields and fields["tags"] is not None:
        sets.append("tags = %s")
        vals.append(fields["tags"])
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.extend([task_id, tenant_id])
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            # If status is changing, validate the transition
            current_status = None
            subtask_warning = None
            if new_status:
                cur.execute(
                    "SELECT status FROM crm_tasks WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                    (task_id, tenant_id),
                )
                row = cur.fetchone()
                if not row:
                    return False
                current_status = row["status"]
                if current_status != new_status:
                    ok, reason = _validate_transition(
                        current_status, new_status, fields.get("resolution")
                    )
                    if not ok:
                        return {"error": reason, "from": current_status, "to": new_status}
                    # Auto-set started_at on first move to IN_PROGRESS
                    if new_status == "IN_PROGRESS":
                        sets.insert(-1, "started_at = COALESCE(started_at, NOW())")
                    # Check subtask completion (soft gate)
                    if new_status in ("REVIEW", "DONE"):
                        subtask_warning = _check_subtask_completion(cur, task_id)

            cur.execute(
                f"UPDATE crm_tasks SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                vals,
            )
            ok_bool: bool = cur.rowcount > 0
            # Record transition history
            if ok_bool and new_status and current_status and current_status != new_status:
                _record_transition(
                    cur,
                    task_id,
                    current_status,
                    new_status,
                    changed_by=changed_by,
                    reason=fields.get("resolution"),
                    metadata={"subtask_warning": subtask_warning} if subtask_warning else None,
                    tenant_id=tenant_id,
                )
            conn.commit()
            if ok_bool:
                _safe_audit("update", "task", task_id, details={"fields": list(fields.keys())})
                if subtask_warning:
                    return {"success": True, "warning": subtask_warning}
            return ok_bool
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update task %s: %s", task_id, e)
            return False


def delete_task(task_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Soft-delete a task."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_tasks SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (task_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("delete", "task", task_id)
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to delete task %s: %s", task_id, e)
            return False


def list_agent_tasks(
    agent_id: str,
    include_unassigned: bool = False,
    status: str | None = None,
    exclude_resolved: bool = True,
    limit: int = 50,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """Get an agent's task inbox, priority-ordered."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL", "tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if include_unassigned:
            conditions.append("(assigned_to_agent = %s OR assigned_to_agent IS NULL)")
            params.append(agent_id)
        else:
            conditions.append("assigned_to_agent = %s")
            params.append(agent_id)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if exclude_resolved and not status:
            conditions.append("status != 'DONE'")
        params.append(limit)
        cur.execute(
            f"""SELECT * FROM crm_tasks
                WHERE {" AND ".join(conditions)}
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'normal' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    due_at ASC NULLS LAST,
                    created_at ASC
                LIMIT %s""",
            params,
        )
        return [task_to_dict(r) for r in cur.fetchall()]


def resolve_task(
    task_id: str,
    resolution: str,
    agent_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> bool | dict[str, Any]:
    """Mark a task as DONE with a resolution summary.

    Returns dict with error if the task has requires_human=True and the caller
    is not ``"philip"`` or ``"helm-user"``.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT status, requires_human FROM crm_tasks WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (task_id, tenant_id),
            )
            row = cur.fetchone()
            from_status = row["status"] if row else None

            # Guard: requires_human tasks can only be resolved by Philip
            if (
                row
                and row.get("requires_human")
                and agent_id not in ("philip", "helm-user", "main")
            ):
                return {
                    "error": "Task requires human resolution — only Philip can resolve it",
                    "id": task_id,
                }

            cur.execute(
                """UPDATE crm_tasks
                   SET status = 'DONE', resolved_at = NOW(), resolution = %s, updated_at = NOW()
                   WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s""",
                (resolution, task_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            if ok and from_status:
                _record_transition(
                    cur,
                    task_id,
                    from_status,
                    "DONE",
                    changed_by=agent_id or "system",
                    reason=resolution,
                    tenant_id=tenant_id,
                )
            conn.commit()
            if ok:
                _safe_audit(
                    "resolve",
                    "task",
                    task_id,
                    details={
                        "resolution": resolution,
                        "agent_id": agent_id,
                    },
                )
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to resolve task %s: %s", task_id, e)
            return False


def approve_task(
    task_id: str,
    resolution: str,
    reviewer: str,
    tenant_id: str = DEFAULT_TENANT,
) -> bool | dict[str, Any]:
    """Approve a task in REVIEW status. Reviewer must differ from assignee."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT status, assigned_to_agent FROM crm_tasks WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (task_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["status"] != "REVIEW":
                return {"error": f"Task is in '{row['status']}', expected REVIEW"}
            if reviewer == row.get("assigned_to_agent"):
                return {"error": "Reviewer cannot be the same as the assignee"}
            if not resolution:
                return {"error": "Resolution is required for approval"}

            cur.execute(
                """UPDATE crm_tasks
                   SET status = 'DONE', resolved_at = NOW(), resolution = %s, updated_at = NOW()
                   WHERE id = %s AND tenant_id = %s""",
                (resolution, task_id, tenant_id),
            )
            _record_transition(
                cur,
                task_id,
                "REVIEW",
                "DONE",
                changed_by=reviewer,
                reason=resolution,
                tenant_id=tenant_id,
            )
            conn.commit()
            _safe_audit("approve", "task", task_id, details={"reviewer": reviewer})

            # Send review_approved notification
            send_notification(
                from_agent=reviewer,
                to_agent=row.get("assigned_to_agent") or "",
                notification_type="review_approved",
                subject=f"Task approved: {task_id}",
                body=resolution,
                task_id=task_id,
                tenant_id=tenant_id,
            )
            return True
        except Exception as e:
            conn.rollback()
            logger.error("Failed to approve task %s: %s", task_id, e)
            return False


def reject_task(
    task_id: str,
    reason: str,
    reviewer: str,
    change_requests: list[str] | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> bool | dict[str, Any]:
    """Reject a task in REVIEW status. Reverts to IN_PROGRESS."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT status, assigned_to_agent FROM crm_tasks WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (task_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["status"] != "REVIEW":
                return {"error": f"Task is in '{row['status']}', expected REVIEW"}
            if not reason:
                return {"error": "Reason is required for rejection"}

            cur.execute(
                """UPDATE crm_tasks
                   SET status = 'IN_PROGRESS', updated_at = NOW()
                   WHERE id = %s AND tenant_id = %s""",
                (task_id, tenant_id),
            )
            _record_transition(
                cur,
                task_id,
                "REVIEW",
                "IN_PROGRESS",
                changed_by=reviewer,
                reason=reason,
                metadata={"change_requests": change_requests} if change_requests else None,
                tenant_id=tenant_id,
            )

            # Create subtasks from change_requests
            if change_requests:
                for cr in change_requests:
                    subtask_id = str(uuid.uuid4())
                    cur.execute(
                        """INSERT INTO crm_tasks (id, title, body, status, parent_task_id,
                                                   assigned_to_agent, created_by_agent,
                                                   priority, tenant_id)
                           VALUES (%s, %s, %s, 'TODO', %s, %s, %s, 'high', %s)""",
                        (
                            subtask_id,
                            cr,
                            f"Change requested by {reviewer}",
                            task_id,
                            row.get("assigned_to_agent"),
                            reviewer,
                            tenant_id,
                        ),
                    )

            conn.commit()
            _safe_audit("reject", "task", task_id, details={"reviewer": reviewer, "reason": reason})

            # Send review_rejected notification
            send_notification(
                from_agent=reviewer,
                to_agent=row.get("assigned_to_agent") or "",
                notification_type="review_rejected",
                subject=f"Task rejected: {task_id}",
                body=reason,
                task_id=task_id,
                metadata={"change_requests": change_requests} if change_requests else {},
                tenant_id=tenant_id,
            )
            return True
        except Exception as e:
            conn.rollback()
            logger.error("Failed to reject task %s: %s", task_id, e)
            return False


def get_task_history(
    task_id: str, limit: int = 50, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """Get transition history for a task, most recent first."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT * FROM crm_task_history
               WHERE task_id = %s AND tenant_id = %s
               ORDER BY created_at DESC
               LIMIT %s""",
            (task_id, tenant_id, limit),
        )
        return [history_to_dict(r) for r in cur.fetchall()]


# ─── Conversations ───────────────────────────────────────────────────────


def list_conversations(
    status: str = "open",
    page: int = 1,
    page_size: int = 25,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """List conversations by status with pagination."""
    offset = (page - 1) * page_size
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT c.*,
                   COALESCE(p.first_name || ' ' || p.last_name, '') AS person_name
            FROM crm_conversations c
            LEFT JOIN crm_people p ON p.id = c.person_id
            WHERE c.status = %s AND c.tenant_id = %s
            ORDER BY c.last_activity_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """,
            (status, tenant_id, page_size, offset),
        )
        return [conversation_to_dict(r) for r in cur.fetchall()]


def get_conversation(
    conversation_id: int, tenant_id: str = DEFAULT_TENANT
) -> dict[str, Any] | None:
    """Get a conversation by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT c.*,
                   COALESCE(p.first_name || ' ' || p.last_name, '') AS person_name
            FROM crm_conversations c
            LEFT JOIN crm_people p ON p.id = c.person_id
            WHERE c.id = %s AND c.tenant_id = %s
        """,
            (conversation_id, tenant_id),
        )
        row = cur.fetchone()
        return conversation_to_dict(row) if row else None


def list_messages(conversation_id: int, tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
    """List all messages in a conversation."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT * FROM crm_messages
            WHERE conversation_id = %s AND tenant_id = %s
            ORDER BY created_at ASC
        """,
            (conversation_id, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def send_message(
    conversation_id: int,
    content: str,
    message_type: str = "outgoing",
    private: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Create a message in a conversation."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            msg_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO crm_messages (id, conversation_id, content, message_type, private, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """,
                (msg_id, conversation_id, content, message_type, private, tenant_id),
            )
            msg = cur.fetchone()

            # Update conversation counters
            cur.execute(
                """
                UPDATE crm_conversations
                SET messages_count = messages_count + 1,
                    last_activity_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
            """,
                (conversation_id,),
            )
            conn.commit()
            return dict(msg) if msg else None
        except Exception as e:
            conn.rollback()
            logger.error("Failed to send message: %s", e)
            return None


def toggle_conversation_status(
    conversation_id: int, status: str, tenant_id: str = DEFAULT_TENANT
) -> bool:
    """Update conversation status (open/resolved/pending/snoozed)."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_conversations SET status = %s, updated_at = NOW() WHERE id = %s AND tenant_id = %s",
                (status, conversation_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to toggle conversation %s: %s", conversation_id, e)
            return False


# ─── Routines ────────────────────────────────────────────────────────────


def _compute_next_run(cron_expr: str, tz_name: str = "America/New_York") -> datetime | None:
    """Compute next run time from a cron expression."""
    try:
        import pytz  # type: ignore[import-untyped]
        from croniter import croniter  # type: ignore[import-untyped]

        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        cron = croniter(cron_expr, now)
        next_dt: datetime = cron.get_next(datetime)
        return next_dt.astimezone(UTC)
    except Exception as e:
        logger.warning("Failed to compute next_run for '%s': %s", cron_expr, e)
        return None


def create_routine(
    title: str,
    cron_expr: str,
    body: str | None = None,
    tz: str = "America/New_York",
    assigned_to_agent: str | None = None,
    priority: str = "normal",
    tags: list[str] | None = None,
    person_id: str | None = None,
    company_id: str | None = None,
    created_by: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a routine. Returns routine UUID."""
    routine_id = str(uuid.uuid4())
    next_run = _compute_next_run(cron_expr, tz)
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO crm_routines
                   (id, title, body, cron_expr, timezone, assigned_to_agent,
                    priority, tags, person_id, company_id, created_by, next_run_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    routine_id,
                    title,
                    body,
                    cron_expr,
                    tz,
                    assigned_to_agent,
                    priority,
                    tags or [],
                    person_id,
                    company_id,
                    created_by,
                    next_run,
                    tenant_id,
                ),
            )
            conn.commit()
            _safe_audit(
                "create", "routine", routine_id, details={"title": title, "cron": cron_expr}
            )
            return routine_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create routine: %s", e)
            return None


def list_routines(
    active_only: bool = True, limit: int = 50, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """List routines, optionally filtered to active only."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL", "tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if active_only:
            conditions.append("active = TRUE")
        params.append(limit)
        cur.execute(
            f"SELECT * FROM crm_routines WHERE {' AND '.join(conditions)} "
            f"ORDER BY next_run_at ASC NULLS LAST LIMIT %s",
            params,
        )
        return [routine_to_dict(r) for r in cur.fetchall()]


def update_routine(routine_id: str, tenant_id: str = DEFAULT_TENANT, **fields: Any) -> bool:
    """Update a routine's fields."""
    col_map = {
        "title": "title",
        "body": "body",
        "cron_expr": "cron_expr",
        "timezone": "timezone",
        "assigned_to_agent": "assigned_to_agent",
        "priority": "priority",
        "active": "active",
        "person_id": "person_id",
        "company_id": "company_id",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if "tags" in fields and fields["tags"] is not None:
        sets.append("tags = %s")
        vals.append(fields["tags"])
    if not sets:
        return False
    # Recompute next_run_at if cron_expr or timezone changed
    if "cron_expr" in fields or "timezone" in fields:
        cron = fields.get("cron_expr")
        tz = fields.get("timezone", "America/New_York")
        if cron:
            next_run = _compute_next_run(cron, tz)
            sets.append("next_run_at = %s")
            vals.append(next_run)
    sets.append("updated_at = NOW()")
    vals.extend([routine_id, tenant_id])
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_routines SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update routine %s: %s", routine_id, e)
            return False


def delete_routine(routine_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Soft-delete a routine."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_routines SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL AND tenant_id = %s",
                (routine_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to delete routine %s: %s", routine_id, e)
            return False


def get_due_routines(tenant_id: str = DEFAULT_TENANT) -> list[dict[str, Any]]:
    """Get routines due for triggering (next_run_at <= NOW(), active, not deleted)."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT r.* FROM crm_routines r
               WHERE r.active = TRUE
                 AND r.deleted_at IS NULL
                 AND r.tenant_id = %s
                 AND r.next_run_at <= NOW()
                 AND NOT EXISTS (
                     SELECT 1 FROM crm_tasks t
                     WHERE t.title = r.title
                       AND t.created_by_agent = 'routine-trigger'
                       AND t.status IN ('TODO', 'IN_PROGRESS', 'REVIEW')
                       AND t.deleted_at IS NULL
                 )
               ORDER BY r.next_run_at ASC""",
            (tenant_id,),
        )
        return [routine_to_dict(r) for r in cur.fetchall()]


def advance_routine(routine_id: str) -> bool:
    """Advance a routine's next_run_at after triggering."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT cron_expr, timezone FROM crm_routines WHERE id = %s",
                (routine_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            next_run = _compute_next_run(row["cron_expr"], row["timezone"] or "America/New_York")
            cur.execute(
                """UPDATE crm_routines
                   SET next_run_at = %s, last_run_at = NOW(), updated_at = NOW()
                   WHERE id = %s""",
                (next_run, routine_id),
            )
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            logger.error("Failed to advance routine %s: %s", routine_id, e)
            return False


# ─── Notifications ───────────────────────────────────────────────────────


def send_notification(
    from_agent: str,
    to_agent: str,
    notification_type: str,
    subject: str,
    body: str | None = None,
    metadata: dict[str, Any] | None = None,
    task_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Send an agent-to-agent notification. Returns notification UUID."""
    notif_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO crm_agent_notifications
                   (id, tenant_id, from_agent, to_agent, notification_type,
                    subject, body, metadata, task_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    notif_id,
                    tenant_id,
                    from_agent,
                    to_agent,
                    notification_type,
                    subject,
                    body,
                    json.dumps(metadata) if metadata else None,
                    task_id,
                ),
            )
            conn.commit()
            _safe_audit(
                "create",
                "notification",
                notif_id,
                details={"from": from_agent, "to": to_agent, "type": notification_type},
            )
            return notif_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to send notification: %s", e)
            return None


def get_agent_inbox(
    agent_id: str,
    unread_only: bool = True,
    type_filter: str | None = None,
    limit: int = 50,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """Get notifications for an agent, ordered by newest first."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["to_agent = %s", "tenant_id = %s"]
        params: list[Any] = [agent_id, tenant_id]
        if unread_only:
            conditions.append("read_at IS NULL")
        if type_filter:
            conditions.append("notification_type = %s")
            params.append(type_filter)
        params.append(limit)
        cur.execute(
            f"""SELECT * FROM crm_agent_notifications
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC
                LIMIT %s""",
            params,
        )
        return [notification_to_dict(r) for r in cur.fetchall()]


def mark_notification_read(notification_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Mark a notification as read."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_agent_notifications SET read_at = NOW() WHERE id = %s AND tenant_id = %s AND read_at IS NULL",
                (notification_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to mark notification %s read: %s", notification_id, e)
            return False


def acknowledge_notification(notification_id: str, tenant_id: str = DEFAULT_TENANT) -> bool:
    """Acknowledge a notification (marks as both read and acknowledged)."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """UPDATE crm_agent_notifications
                   SET read_at = COALESCE(read_at, NOW()), acknowledged_at = NOW()
                   WHERE id = %s AND tenant_id = %s AND acknowledged_at IS NULL""",
                (notification_id, tenant_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to ack notification %s: %s", notification_id, e)
            return False


def list_notifications(
    from_agent: str | None = None,
    to_agent: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """List notifications with optional filters."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if from_agent:
            conditions.append("from_agent = %s")
            params.append(from_agent)
        if to_agent:
            conditions.append("to_agent = %s")
            params.append(to_agent)
        if task_id:
            conditions.append("task_id = %s")
            params.append(task_id)
        params.append(limit)
        cur.execute(
            f"""SELECT * FROM crm_agent_notifications
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC
                LIMIT %s""",
            params,
        )
        return [notification_to_dict(r) for r in cur.fetchall()]


# ─── Tenants ─────────────────────────────────────────────────────────────


def create_tenant(
    tenant_id: str,
    display_name: str,
    parent_tenant_id: str | None = None,
    settings: dict[str, Any] | None = None,
) -> str | None:
    """Create a tenant. Returns tenant ID."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO crm_tenants (id, display_name, parent_tenant_id, settings)
                   VALUES (%s, %s, %s, %s)""",
                (tenant_id, display_name, parent_tenant_id, json.dumps(settings or {})),
            )
            conn.commit()
            _safe_audit("create", "tenant", tenant_id, details={"display_name": display_name})
            return tenant_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create tenant: %s", e)
            return None


def get_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Get a tenant by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM crm_tenants WHERE id = %s", (tenant_id,))
        row = cur.fetchone()
        return tenant_to_dict(row) if row else None


def list_tenants(parent_id: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    """List tenants, optionally filtered by parent."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions: list[str] = []
        params: list[Any] = []
        if active_only:
            conditions.append("active = TRUE")
        if parent_id:
            conditions.append("parent_tenant_id = %s")
            params.append(parent_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f"SELECT * FROM crm_tenants {where} ORDER BY display_name",
            params,
        )
        return [tenant_to_dict(r) for r in cur.fetchall()]


def update_tenant(tenant_id: str, **fields: Any) -> bool:
    """Update a tenant's fields."""
    col_map = {
        "display_name": "display_name",
        "parent_tenant_id": "parent_tenant_id",
        "active": "active",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if "settings" in fields and fields["settings"] is not None:
        sets.append("settings = %s::jsonb")
        vals.append(json.dumps(fields["settings"]))
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.append(tenant_id)
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_tenants SET {', '.join(sets)} WHERE id = %s",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update tenant %s: %s", tenant_id, e)
            return False


# ─── Shared Working State ────────────────────────────────────────────────


def append_to_block(block_name: str, entry: str, max_entries: int = 20) -> bool:
    """Append a timestamped line to a memory block, trimming oldest entries."""
    timestamp = datetime.now(UTC).strftime("%H:%M")
    line = f"[{timestamp}] {entry}"
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT content FROM agent_memory_blocks WHERE block_name = %s",
                (block_name,),
            )
            row = cur.fetchone()
            if row and row[0]:
                lines = row[0].strip().split("\n")
            else:
                lines = []

            lines.append(line)
            # Trim to max_entries
            if len(lines) > max_entries:
                lines = lines[-max_entries:]

            new_content = "\n".join(lines)
            cur.execute(
                """INSERT INTO agent_memory_blocks (block_name, content, last_written_at, write_count)
                   VALUES (%s, %s, NOW(), 1)
                   ON CONFLICT (block_name) DO UPDATE
                   SET content = EXCLUDED.content, last_written_at = NOW(),
                       write_count = agent_memory_blocks.write_count + 1""",
                (block_name, new_content),
            )
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            logger.error("Failed to append to block %s: %s", block_name, e)
            return False


# ─── Search & Metadata ───────────────────────────────────────────────────


def get_metadata_objects() -> list[dict[str, Any]]:
    """Return list of CRM table names and labels."""
    return [
        {"name": "crm_people", "label": "People"},
        {"name": "crm_companies", "label": "Companies"},
        {"name": "crm_notes", "label": "Notes"},
        {"name": "crm_tasks", "label": "Tasks"},
        {"name": "crm_conversations", "label": "Conversations"},
        {"name": "crm_messages", "label": "Messages"},
        {"name": "crm_tenants", "label": "Tenants"},
        {"name": "crm_agent_notifications", "label": "Notifications"},
    ]


def get_object_metadata(object_name: str) -> dict[str, Any] | None:
    """Return column info for a CRM table."""
    valid = {
        "crm_people",
        "crm_companies",
        "crm_notes",
        "crm_tasks",
        "crm_conversations",
        "crm_messages",
        "crm_tenants",
        "crm_agent_notifications",
    }
    if object_name not in valid:
        return None

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """,
            (object_name,),
        )
        cols = cur.fetchall()
        return {"name": object_name, "columns": [dict(c) for c in cols]}


def search_records(
    query: str, object_name: str | None = None, limit: int = 20, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """Cross-table keyword search on CRM entities."""
    results: list[dict[str, Any]] = []
    pattern = f"%{query}%"

    tables = {
        "crm_people": ("first_name", "last_name", "email"),
        "crm_companies": ("name", "domain_name"),
        "crm_notes": ("title", "body"),
        "crm_tasks": ("title", "body"),
    }

    if object_name and object_name in tables:
        tables = {object_name: tables[object_name]}

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for table, cols in tables.items():
            conditions = " OR ".join(f"{c} ILIKE %s" for c in cols)
            params = [pattern] * len(cols)
            cur.execute(
                f"SELECT *, '{table}' as _table FROM {table} "
                f"WHERE deleted_at IS NULL AND tenant_id = %s AND ({conditions}) "
                f"ORDER BY updated_at DESC LIMIT %s",
                [tenant_id, *params, limit],
            )
            results.extend(dict(r) for r in cur.fetchall())

    return results[:limit]


def check_health() -> dict[str, Any]:
    """Quick health check: count people + conversations."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM crm_people WHERE deleted_at IS NULL")
        people_count: int = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM crm_conversations")
        conv_count: int = cur.fetchone()[0]
        return {"status": "ok", "people": people_count, "conversations": conv_count}


# ─── Contact Resolution ──────────────────────────────────────────────────


def resolve_contact(
    channel: str,
    identifier: str,
    name: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Resolve a channel identifier to a person_id. Creates person if needed.

    Upserts into ``contact_identifiers`` and returns the resolved row.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Check existing mapping
        cur.execute(
            "SELECT * FROM contact_identifiers WHERE channel = %s AND identifier = %s",
            (channel, identifier),
        )
        existing = cur.fetchone()

        person_id = None
        display = name or (existing["display_name"] if existing else identifier)

        if existing:
            person_id = existing.get("person_id")

        # If no person_id, search or create
        if not person_id:
            search_term = name or identifier
            people = search_people(search_term, tenant_id=tenant_id)
            if people:
                person_id = people[0]["id"]
            elif name:
                parts = name.split(None, 1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
                email = identifier if channel == "email" else None
                phone = identifier if channel in ("voice", "sms") else None
                person_id = create_person(
                    first, last, email=email, phone=phone, tenant_id=tenant_id
                )

        # Upsert the mapping
        cur.execute(
            """
            INSERT INTO contact_identifiers (channel, identifier, display_name, person_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (channel, identifier) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, contact_identifiers.display_name),
                person_id = COALESCE(EXCLUDED.person_id, contact_identifiers.person_id),
                updated_at = NOW()
            RETURNING *
            """,
            (channel, identifier, display, person_id),
        )
        result = cur.fetchone()
        conn.commit()

        _safe_audit(
            "resolve",
            "contact",
            str(person_id) if person_id else None,
            details={
                "channel": channel,
                "identifier": identifier,
                "name": name,
                "person_id": str(person_id) if person_id else None,
                "existed": existing is not None,
            },
        )

        return dict(result) if result else {}


def get_conversations_for_contact(
    person_id: str, tenant_id: str = DEFAULT_TENANT
) -> list[dict[str, Any]]:
    """Get all conversations for a person, newest first."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT * FROM crm_conversations
            WHERE person_id = %s AND tenant_id = %s
            ORDER BY last_activity_at DESC NULLS LAST
            """,
            (person_id, tenant_id),
        )
        return [conversation_to_dict(r) for r in cur.fetchall()]


def create_conversation(
    person_id: str,
    inbox_name: str = "Robothor Bridge",
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any] | None:
    """Create a new conversation for a person."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                """
                INSERT INTO crm_conversations (person_id, status, inbox_name, last_activity_at, tenant_id)
                VALUES (%s, 'open', %s, NOW(), %s)
                RETURNING *
                """,
                (person_id, inbox_name, tenant_id),
            )
            row = cur.fetchone()
            conn.commit()
            if row:
                _safe_audit(
                    "create",
                    "conversation",
                    str(row["id"]),
                    details={"person_id": person_id, "inbox_name": inbox_name},
                )
            return conversation_to_dict(row) if row else None
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create conversation: %s", e)
            return None


def get_timeline(identifier: str, tenant_id: str = DEFAULT_TENANT) -> dict[str, Any]:
    """Get unified timeline for a contact across CRM data.

    Looks up a contact by identifier (email, phone, name), then gathers
    their person record and conversation history.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Find all identifiers for this person
        cur.execute(
            "SELECT * FROM contact_identifiers WHERE identifier = %s OR display_name ILIKE %s",
            (identifier, f"%{identifier}%"),
        )
        mappings = cur.fetchall()

        timeline: dict[str, Any] = {
            "identifier": identifier,
            "mappings": [dict(m) for m in mappings],
            "conversations": [],
        }

        if not mappings:
            return timeline

        # Get person data
        for m in mappings:
            pid = m.get("person_id")
            if pid:
                person = get_person(str(pid), tenant_id=tenant_id)
                if person:
                    timeline["person"] = person
                    break

        # Get conversations
        for m in mappings:
            pid = m.get("person_id")
            if pid:
                convos = get_conversations_for_contact(str(pid), tenant_id=tenant_id)
                timeline["conversations"] = convos
                break

        return timeline
