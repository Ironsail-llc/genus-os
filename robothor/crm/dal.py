"""
CRM Data Access Layer — PostgreSQL CRUD for all CRM entities.

All operations use soft deletes (deleted_at). All mutations are audit-logged.
Response shapes are defined in robothor.crm.models.

Usage:
    from robothor.crm.dal import create_person, search_people, get_person

    person_id = create_person("Philip", "Ironsail", email="philip@example.com")
    people = search_people("Philip")
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.crm.models import (
    company_to_dict,
    conversation_to_dict,
    note_to_dict,
    person_to_dict,
    task_to_dict,
)
from robothor.crm.validation import (
    COMPANY_BLOCKLIST,
    normalize_email,
    scrub_null_string,
    validate_person_input,
)
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


def _safe_audit(operation: str, entity_type: str, entity_id: str | None, **kwargs: Any) -> None:
    """Wrap audit logging so it never propagates exceptions."""
    try:
        from robothor.audit.logger import log_crm_mutation
        log_crm_mutation(operation, entity_type, entity_id, **kwargs)
    except Exception as e:
        logger.warning("Audit call failed (non-fatal): %s", e)


# ─── People ──────────────────────────────────────────────────────────────


def search_people(name: str) -> list[dict]:
    """Search people by name (ILIKE on first_name/last_name)."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        pattern = f"%{name}%"
        cur.execute("""
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.deleted_at IS NULL
              AND (p.first_name ILIKE %s OR p.last_name ILIKE %s)
            ORDER BY p.updated_at DESC
            LIMIT 50
        """, (pattern, pattern))
        return [person_to_dict(r) for r in cur.fetchall()]


def create_person(
    first_name: str,
    last_name: str = "",
    email: str | None = None,
    phone: str | None = None,
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
            cur.execute("""
                INSERT INTO crm_people (id, first_name, last_name, email, phone)
                VALUES (%s, %s, %s, %s, %s)
            """, (person_id, first_name, last_name, email, phone))
            conn.commit()
            _safe_audit(
                "create", "person", person_id,
                details={"first_name": first_name, "last_name": last_name,
                         "email": email, "phone": phone},
            )
            return person_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create person: %s", e)
            return None


def get_person(person_id: str) -> dict | None:
    """Get a person by ID, with company JOIN."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.id = %s AND p.deleted_at IS NULL
        """, (person_id,))
        row = cur.fetchone()
        return person_to_dict(row) if row else None


def update_person(person_id: str, **fields: Any) -> bool:
    """Update a person's fields. Only sets non-None fields."""
    for key in ("job_title", "city", "first_name", "last_name"):
        if key in fields and fields[key] is not None:
            fields[key] = scrub_null_string(fields[key])

    col_map = {
        "job_title": "job_title", "company_id": "company_id",
        "city": "city", "linkedin_url": "linkedin_url",
        "phone": "phone", "avatar_url": "avatar_url",
        "first_name": "first_name", "last_name": "last_name",
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
    vals.append(person_id)

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_people SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
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


def delete_person(person_id: str) -> bool:
    """Soft-delete a person."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_people SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL",
                (person_id,),
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


def merge_people(keeper_id: str, loser_id: str) -> dict | None:
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
            cur.execute("SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL", (keeper_id,))
            keeper = cur.fetchone()
            cur.execute("SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL", (loser_id,))
            loser = cur.fetchone()

            if not keeper or not loser:
                logger.error("merge_people: keeper or loser not found")
                return None

            # 1. Fill empty fields
            fillable = ["first_name", "last_name", "email", "phone", "job_title",
                         "city", "linkedin_url", "avatar_url", "company_id"]
            updates: list[str] = []
            update_vals: list[Any] = []
            for field in fillable:
                keeper_val = keeper.get(field)
                loser_val = loser.get(field)
                keeper_empty = not keeper_val or (isinstance(keeper_val, str) and not keeper_val.strip())
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
            cur.execute("UPDATE contact_identifiers SET person_id = %s WHERE person_id = %s",
                        (keeper_id, loser_id))
            cur.execute("UPDATE crm_conversations SET person_id = %s WHERE person_id = %s",
                        (keeper_id, loser_id))
            cur.execute("UPDATE crm_notes SET person_id = %s WHERE person_id = %s AND deleted_at IS NULL",
                        (keeper_id, loser_id))
            cur.execute("UPDATE crm_tasks SET person_id = %s WHERE person_id = %s AND deleted_at IS NULL",
                        (keeper_id, loser_id))

            # 4. Soft-delete loser
            cur.execute("UPDATE crm_people SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s",
                        (loser_id,))

            # 5. Create merge note
            loser_name = f"{loser.get('first_name', '')} {loser.get('last_name', '')}".strip()
            note_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO crm_notes (id, title, body, person_id) VALUES (%s, %s, %s, %s)",
                (note_id, "Duplicate Merged",
                 f"Merged duplicate: {loser_name} (id: {loser_id})", keeper_id),
            )

            conn.commit()
            _safe_audit("merge", "person", keeper_id,
                        details={"loser_id": loser_id, "loser_name": loser_name})
            return get_person(keeper_id)

        except Exception as e:
            conn.rollback()
            logger.error("Failed to merge person %s into %s: %s", loser_id, keeper_id, e)
            return None


def merge_companies(keeper_id: str, loser_id: str) -> dict | None:
    """Merge loser company into keeper.

    1. Fill keeper's empty fields from loser
    2. Re-point crm_people.company_id, crm_notes.company_id
    3. Soft-delete loser
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL", (keeper_id,))
            keeper = cur.fetchone()
            cur.execute("SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL", (loser_id,))
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
                keeper_empty = not keeper_val or (isinstance(keeper_val, str) and not keeper_val.strip())
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

            cur.execute("UPDATE crm_people SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
                        (keeper_id, loser_id))
            cur.execute("UPDATE crm_notes SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
                        (keeper_id, loser_id))
            cur.execute("UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s",
                        (loser_id,))

            conn.commit()
            _safe_audit("merge", "company", keeper_id,
                        details={"loser_id": loser_id, "loser_name": loser.get("name", "")})
            return get_company(keeper_id)

        except Exception as e:
            conn.rollback()
            logger.error("Failed to merge company %s into %s: %s", loser_id, keeper_id, e)
            return None


def list_people(search: str | None = None, limit: int = 20) -> list[dict]:
    """List people, optionally filtered by search term."""
    if search:
        return search_people(search)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT p.*, c.name AS company_name
            FROM crm_people p
            LEFT JOIN crm_companies c ON c.id = p.company_id
            WHERE p.deleted_at IS NULL
            ORDER BY p.updated_at DESC LIMIT %s
        """, (limit,))
        return [person_to_dict(r) for r in cur.fetchall()]


# ─── Companies ───────────────────────────────────────────────────────────


def find_or_create_company(name: str) -> str | None:
    """Find a company by name (ILIKE), or create it. Returns company UUID."""
    if name.strip().lower() in COMPANY_BLOCKLIST:
        logger.info("Blocked find_or_create_company(%s): in blocklist", name)
        return None

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id FROM crm_companies WHERE name ILIKE %s AND deleted_at IS NULL LIMIT 1",
            (f"%{name}%",),
        )
        row = cur.fetchone()
        if row:
            result: str = str(row["id"])
            return result

        company_id = str(uuid.uuid4())
        try:
            cur.execute("INSERT INTO crm_companies (id, name) VALUES (%s, %s)", (company_id, name))
            conn.commit()
            _safe_audit("create", "company", company_id, details={"name": name, "via": "find_or_create"})
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
) -> str | None:
    """Create a company. Returns company UUID."""
    company_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO crm_companies (id, name, domain_name, employees,
                    address, linkedin_url, ideal_customer_profile)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (company_id, name, domain_name, employees, address,
                  linkedin_url, ideal_customer_profile))
            conn.commit()
            _safe_audit("create", "company", company_id, details={"name": name})
            return company_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create company: %s", e)
            return None


def get_company(company_id: str) -> dict | None:
    """Get a company by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL",
            (company_id,),
        )
        row = cur.fetchone()
        return company_to_dict(row) if row else None


def update_company(company_id: str, **fields: Any) -> bool:
    """Update a company's fields. Only sets non-None fields."""
    col_map = {
        "domain_name": "domain_name", "employees": "employees",
        "address": "address", "linkedin_url": "linkedin_url",
        "ideal_customer_profile": "ideal_customer_profile", "name": "name",
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
    vals.append(company_id)

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_companies SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("update", "company", company_id, details={"fields": list(fields.keys())})
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update company %s: %s", company_id, e)
            return False


def delete_company(company_id: str) -> bool:
    """Soft-delete a company."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL",
                (company_id,),
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


def list_companies(search: str | None = None, limit: int = 50) -> list[dict]:
    """List companies, optionally filtered by name search."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if search:
            cur.execute("""
                SELECT * FROM crm_companies
                WHERE deleted_at IS NULL AND name ILIKE %s
                ORDER BY updated_at DESC LIMIT %s
            """, (f"%{search}%", limit))
        else:
            cur.execute("""
                SELECT * FROM crm_companies
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT %s
            """, (limit,))
        return [company_to_dict(r) for r in cur.fetchall()]


# ─── Notes ───────────────────────────────────────────────────────────────


def create_note(
    title: str,
    body: str,
    person_id: str | None = None,
    company_id: str | None = None,
) -> str | None:
    """Create a note. Returns note UUID."""
    note_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO crm_notes (id, title, body, person_id, company_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (note_id, title, body, person_id, company_id))
            conn.commit()
            _safe_audit("create", "note", note_id, details={"title": title})
            return note_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create note: %s", e)
            return None


def get_note(note_id: str) -> dict | None:
    """Get a note by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_notes WHERE id = %s AND deleted_at IS NULL", (note_id,),
        )
        row = cur.fetchone()
        return note_to_dict(row) if row else None


def list_notes(
    person_id: str | None = None,
    company_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List notes with optional person/company filter."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
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


def update_note(note_id: str, **fields: Any) -> bool:
    """Update a note. Accepted: title, body, person_id, company_id."""
    col_map = {"title": "title", "body": "body", "person_id": "person_id", "company_id": "company_id"}
    sets: list[str] = []
    vals: list[Any] = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.append(note_id)
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_notes SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
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


def delete_note(note_id: str) -> bool:
    """Soft-delete a note."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_notes SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL",
                (note_id,),
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


def create_task(
    title: str,
    body: str | None = None,
    status: str = "TODO",
    due_at: str | None = None,
    person_id: str | None = None,
    company_id: str | None = None,
) -> str | None:
    """Create a task. Returns task UUID."""
    task_id = str(uuid.uuid4())
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO crm_tasks (id, title, body, status, due_at, person_id, company_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (task_id, title, body, status, due_at, person_id, company_id))
            conn.commit()
            _safe_audit("create", "task", task_id, details={"title": title})
            return task_id
        except Exception as e:
            conn.rollback()
            logger.error("Failed to create task: %s", e)
            return None


def get_task(task_id: str) -> dict | None:
    """Get a task by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM crm_tasks WHERE id = %s AND deleted_at IS NULL", (task_id,),
        )
        row = cur.fetchone()
        return task_to_dict(row) if row else None


def list_tasks(
    status: str | None = None,
    person_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks with optional status/person filter."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if status:
            conditions.append("status = %s")
            params.append(status)
        if person_id:
            conditions.append("person_id = %s")
            params.append(person_id)
        params.append(limit)
        cur.execute(
            f"SELECT * FROM crm_tasks WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT %s",
            params,
        )
        return [task_to_dict(r) for r in cur.fetchall()]


def update_task(task_id: str, **fields: Any) -> bool:
    """Update a task. Accepted: title, body, status, due_at, person_id, company_id."""
    col_map = {
        "title": "title", "body": "body", "status": "status",
        "due_at": "due_at", "person_id": "person_id", "company_id": "company_id",
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
    vals.append(task_id)
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE crm_tasks SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
                vals,
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            if ok:
                _safe_audit("update", "task", task_id, details={"fields": list(fields.keys())})
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to update task %s: %s", task_id, e)
            return False


def delete_task(task_id: str) -> bool:
    """Soft-delete a task."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_tasks SET deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL",
                (task_id,),
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


# ─── Conversations ───────────────────────────────────────────────────────


def list_conversations(
    status: str = "open",
    page: int = 1,
    page_size: int = 25,
) -> list[dict]:
    """List conversations by status with pagination."""
    offset = (page - 1) * page_size
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT c.*,
                   COALESCE(p.first_name || ' ' || p.last_name, '') AS person_name
            FROM crm_conversations c
            LEFT JOIN crm_people p ON p.id = c.person_id
            WHERE c.status = %s
            ORDER BY c.last_activity_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, (status, page_size, offset))
        return [conversation_to_dict(r) for r in cur.fetchall()]


def get_conversation(conversation_id: int) -> dict | None:
    """Get a conversation by ID."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT c.*,
                   COALESCE(p.first_name || ' ' || p.last_name, '') AS person_name
            FROM crm_conversations c
            LEFT JOIN crm_people p ON p.id = c.person_id
            WHERE c.id = %s
        """, (conversation_id,))
        row = cur.fetchone()
        return conversation_to_dict(row) if row else None


def list_messages(conversation_id: int) -> list[dict]:
    """List all messages in a conversation."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM crm_messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
        """, (conversation_id,))
        return [dict(r) for r in cur.fetchall()]


def send_message(
    conversation_id: int,
    content: str,
    message_type: str = "outgoing",
    private: bool = False,
) -> dict | None:
    """Create a message in a conversation."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            msg_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO crm_messages (id, conversation_id, content, message_type, private)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
            """, (msg_id, conversation_id, content, message_type, private))
            msg = cur.fetchone()

            # Update conversation counters
            cur.execute("""
                UPDATE crm_conversations
                SET messages_count = messages_count + 1,
                    last_activity_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
            """, (conversation_id,))
            conn.commit()
            return dict(msg) if msg else None
        except Exception as e:
            conn.rollback()
            logger.error("Failed to send message: %s", e)
            return None


def toggle_conversation_status(conversation_id: int, status: str) -> bool:
    """Update conversation status (open/resolved/pending/snoozed)."""
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE crm_conversations SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, conversation_id),
            )
            ok: bool = cur.rowcount > 0
            conn.commit()
            return ok
        except Exception as e:
            conn.rollback()
            logger.error("Failed to toggle conversation %s: %s", conversation_id, e)
            return False


# ─── Search & Metadata ───────────────────────────────────────────────────


def get_metadata_objects() -> list[dict]:
    """Return list of CRM table names and labels."""
    return [
        {"name": "crm_people", "label": "People"},
        {"name": "crm_companies", "label": "Companies"},
        {"name": "crm_notes", "label": "Notes"},
        {"name": "crm_tasks", "label": "Tasks"},
        {"name": "crm_conversations", "label": "Conversations"},
        {"name": "crm_messages", "label": "Messages"},
    ]


def get_object_metadata(object_name: str) -> dict | None:
    """Return column info for a CRM table."""
    valid = {"crm_people", "crm_companies", "crm_notes", "crm_tasks",
             "crm_conversations", "crm_messages"}
    if object_name not in valid:
        return None

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """, (object_name,))
        cols = cur.fetchall()
        return {"name": object_name, "columns": [dict(c) for c in cols]}


def search_records(query: str, object_name: str | None = None, limit: int = 20) -> list[dict]:
    """Cross-table keyword search on CRM entities."""
    results: list[dict] = []
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
                f"WHERE deleted_at IS NULL AND ({conditions}) "
                f"ORDER BY updated_at DESC LIMIT %s",
                [*params, limit],
            )
            results.extend(dict(r) for r in cur.fetchall())

    return results[:limit]


def check_health() -> dict:
    """Quick health check: count people + conversations."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM crm_people WHERE deleted_at IS NULL")
        people_count: int = cur.fetchone()[0]  # type: ignore[index]
        cur.execute("SELECT COUNT(*) FROM crm_conversations")
        conv_count: int = cur.fetchone()[0]  # type: ignore[index]
        return {"status": "ok", "people": people_count, "conversations": conv_count}
