"""
CRM Data Access Layer — Direct PostgreSQL access for all CRM operations.

All CRM operations via SQL against crm_* tables in robothor_memory.
Response formats are used by bridge_service.py, mcp_server.py, and crm_fetcher.py.

Uses psycopg2 + RealDictCursor, matching existing codebase patterns.
"""

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

import config

# Audit logging — import from memory_system
sys.path.insert(0, "/home/philip/clawd/memory_system")
import audit

logger = logging.getLogger(__name__)


def _safe_audit(operation, entity_type, entity_id, **kwargs):
    """Wrap audit.log_crm_mutation so it never propagates exceptions."""
    try:
        return audit.log_crm_mutation(operation, entity_type, entity_id, **kwargs)
    except Exception as e:
        logger.warning("Audit call failed (non-fatal): %s", e)
        return None


# ─── Blocklists & Validation ──────────────────────────────────────────────

PERSON_BLOCKLIST = {
    # Furniture / objects misidentified as people
    "couch", "chair", "table", "desk", "lamp", "sofa", "bed", "shelf",
    "door", "window", "wall", "floor", "ceiling", "cabinet", "dresser",
    # Bot / system accounts
    "claude", "vision monitor system", "robothor vision monitor",
    "chatwoot inbox monitor", "chatwoot monitor", "robothor system",
    "email responder", "human resources", "gemini (google workspace)",
    "gemini notes", "google meet", "linkedin (automated)",
    "linkedin (noreply)", "gitguardian", "openrouter team",
}

COMPANY_BLOCKLIST = {
    "null", "none", "unknown", "test", "n/a",
}


def _scrub_null_string(value: str | None) -> str | None:
    """Replace literal 'null' strings with empty string."""
    if value is None:
        return None
    if value.strip().lower() in ("null", "none", "n/a"):
        return ""
    return value


def validate_person_input(first_name: str, last_name: str = "",
                          email: str | None = None) -> tuple[bool, str]:
    """Validate person input against blocklist and basic rules.

    Returns (is_valid, reason).
    """
    full_name = f"{first_name} {last_name}".strip().lower()

    # Blocklist check
    if full_name in PERSON_BLOCKLIST:
        return False, f"blocked: '{full_name}' is in the person blocklist"
    if first_name.strip().lower() in PERSON_BLOCKLIST:
        return False, f"blocked: '{first_name}' is in the person blocklist"

    # Reject literal null strings
    if first_name.strip().lower() in ("null", "none", "n/a"):
        return False, "rejected: first_name is a null-like string"

    # Name too short
    if len(first_name.strip()) < 2:
        return False, "rejected: first_name must be at least 2 characters"

    # Email validation
    if email and "@" not in email:
        return False, "rejected: email must contain '@'"

    return True, "ok"


def _conn():
    """Get a database connection."""
    return psycopg2.connect(config.PG_DSN)


def _now():
    return datetime.now(timezone.utc)


# ─── People ──────────────────────────────────────────────────────────────


def _person_to_twenty_shape(row: dict) -> dict:
    """Convert a crm_people row to Twenty GraphQL response shape."""
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


def search_people(name: str) -> list:
    """Search people by name (ILIKE on first_name/last_name)."""
    conn = _conn()
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
    rows = cur.fetchall()
    conn.close()
    return [_person_to_twenty_shape(r) for r in rows]


def create_person(first_name: str, last_name: str,
                  email: str | None = None, phone: str | None = None) -> str | None:
    """Create a person. Returns person UUID, or None if blocked/invalid."""
    valid, reason = validate_person_input(first_name, last_name, email)
    if not valid:
        logger.info("Blocked create_person(%s %s): %s", first_name, last_name, reason)
        return None

    # Normalize
    email = email.lower().strip() if email else None
    first_name = _scrub_null_string(first_name) or first_name
    last_name = _scrub_null_string(last_name) or last_name

    person_id = str(uuid.uuid4())
    conn = _conn()
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
        _safe_audit(
            "create", "person", None,
            details={"first_name": first_name, "error": str(e)},
            status="error",
        )
        return None
    finally:
        conn.close()


def get_person(person_id: str) -> dict | None:
    """Get a person by ID, with company JOIN."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT p.*, c.name AS company_name
        FROM crm_people p
        LEFT JOIN crm_companies c ON c.id = p.company_id
        WHERE p.id = %s AND p.deleted_at IS NULL
    """, (person_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return _person_to_twenty_shape(row)
    return None


def update_person(person_id: str, **fields) -> bool:
    """Update a person's fields. Only sets non-None fields.

    Accepted fields: job_title, company_id, city, linkedin_url, phone, avatar_url,
                     first_name, last_name, email, additional_emails, additional_phones.
    """
    # Scrub null-like strings for text fields
    for key in ("job_title", "city", "first_name", "last_name"):
        if key in fields and fields[key] is not None:
            fields[key] = _scrub_null_string(fields[key])

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
    sets = []
    vals = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])

    # JSONB fields
    for jsonb_field in ("additional_emails", "additional_phones"):
        if jsonb_field in fields and fields[jsonb_field] is not None:
            sets.append(f"{jsonb_field} = %s::jsonb")
            vals.append(json.dumps(fields[jsonb_field]))

    if not sets:
        return False

    sets.append("updated_at = NOW()")
    vals.append(person_id)

    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE crm_people SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
            vals,
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit(
                "update", "person", person_id,
                details={"fields": list(fields.keys())},
            )
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to update person %s: %s", person_id, e)
        return False
    finally:
        conn.close()


def delete_person(person_id: str) -> bool:
    """Soft-delete a person."""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE crm_people SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (person_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit("delete", "person", person_id)
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to delete person %s: %s", person_id, e)
        return False
    finally:
        conn.close()


def list_people(search: str | None = None, limit: int = 20) -> list:
    """List people, optionally filtered by search term."""
    if search:
        return search_people(search)

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT p.*, c.name AS company_name
        FROM crm_people p
        LEFT JOIN crm_companies c ON c.id = p.company_id
        WHERE p.deleted_at IS NULL
        ORDER BY p.updated_at DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_person_to_twenty_shape(r) for r in rows]


# ─── Companies ───────────────────────────────────────────────────────────


def _company_to_dict(row: dict) -> dict:
    """Convert a crm_companies row to response dict."""
    return {
        "id": str(row["id"]),
        "name": row.get("name") or "",
        "domainName": row.get("domain_name") or "",
        "employees": row.get("employees"),
        "address": row.get("address_street1") or "",
        "addressCity": row.get("address_city") or "",
        "addressState": row.get("address_state") or "",
        "linkedinUrl": row.get("linkedin_url") or "",
        "idealCustomerProfile": row.get("ideal_customer_profile", False),
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def find_or_create_company(name: str) -> str | None:
    """Find a company by name (ILIKE), or create it. Returns company UUID."""
    if name.strip().lower() in COMPANY_BLOCKLIST:
        logger.info("Blocked find_or_create_company(%s): in blocklist", name)
        return None

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    pattern = f"%{name}%"
    cur.execute(
        "SELECT id FROM crm_companies WHERE name ILIKE %s AND deleted_at IS NULL LIMIT 1",
        (pattern,),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return str(row["id"])

    # Create
    company_id = str(uuid.uuid4())
    try:
        cur.execute(
            "INSERT INTO crm_companies (id, name) VALUES (%s, %s)",
            (company_id, name),
        )
        conn.commit()
        _safe_audit(
            "create", "company", company_id,
            details={"name": name, "via": "find_or_create"},
        )
        return company_id
    except Exception as e:
        conn.rollback()
        logger.error("Failed to create company: %s", e)
        return None
    finally:
        conn.close()


def create_company(name: str, domain_name: str | None = None,
                   employees: int | None = None, address: str | None = None,
                   linkedin_url: str | None = None,
                   ideal_customer_profile: bool = False) -> str | None:
    """Create a company. Returns company UUID."""
    company_id = str(uuid.uuid4())
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO crm_companies (id, name, domain_name, employees, address_street1,
                                       linkedin_url, ideal_customer_profile)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (company_id, name, domain_name, employees, address,
              linkedin_url, ideal_customer_profile))
        conn.commit()
        _safe_audit(
            "create", "company", company_id,
            details={"name": name, "domain_name": domain_name},
        )
        return company_id
    except Exception as e:
        conn.rollback()
        logger.error("Failed to create company: %s", e)
        return None
    finally:
        conn.close()


def get_company(company_id: str) -> dict | None:
    """Get a company by ID."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL",
        (company_id,),
    )
    row = cur.fetchone()
    conn.close()
    return _company_to_dict(row) if row else None


def update_company(company_id: str, **fields) -> bool:
    """Update a company's fields. Only sets non-None fields.

    Accepted: domain_name, employees, address, linkedin_url, ideal_customer_profile, name.
    """
    col_map = {
        "domain_name": "domain_name",
        "employees": "employees",
        "address": "address_street1",
        "linkedin_url": "linkedin_url",
        "ideal_customer_profile": "ideal_customer_profile",
        "name": "name",
    }
    sets = []
    vals = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])

    if not sets:
        return False

    sets.append("updated_at = NOW()")
    vals.append(company_id)

    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE crm_companies SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL",
            vals,
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit(
                "update", "company", company_id,
                details={"fields": list(fields.keys())},
            )
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to update company %s: %s", company_id, e)
        return False
    finally:
        conn.close()


def delete_company(company_id: str) -> bool:
    """Soft-delete a company."""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (company_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit("delete", "company", company_id)
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to delete company %s: %s", company_id, e)
        return False
    finally:
        conn.close()


def list_companies(search: str | None = None, limit: int = 50) -> list:
    """List companies, optionally filtered by name search."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if search:
        pattern = f"%{search}%"
        cur.execute("""
            SELECT * FROM crm_companies
            WHERE deleted_at IS NULL AND name ILIKE %s
            ORDER BY updated_at DESC LIMIT %s
        """, (pattern, limit))
    else:
        cur.execute("""
            SELECT * FROM crm_companies
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC LIMIT %s
        """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [_company_to_dict(r) for r in rows]


# ─── Merge Operations ────────────────────────────────────────────────────


def merge_people(keeper_id: str, loser_id: str) -> dict | None:
    """Merge loser into keeper in a single transaction.

    1. Fill keeper's empty fields from loser
    2. Collect loser's emails/phones into keeper's additional_emails/phones JSONB
    3. Re-point contact_identifiers, conversations, notes, tasks
    4. Soft-delete loser
    5. Create merge note on keeper
    Returns keeper record on success, None on failure.
    """
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Fetch both records
        cur.execute("SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL", (keeper_id,))
        keeper = cur.fetchone()
        cur.execute("SELECT * FROM crm_people WHERE id = %s AND deleted_at IS NULL", (loser_id,))
        loser = cur.fetchone()

        if not keeper:
            logger.error("merge_people: keeper %s not found", keeper_id)
            return None
        if not loser:
            logger.warning("merge_people: loser %s not found or already deleted", loser_id)
            return None

        # 1. Fill keeper's empty fields from loser
        fillable = ["first_name", "last_name", "email", "phone", "job_title",
                     "city", "linkedin_url", "avatar_url", "company_id"]
        updates = []
        update_vals = []
        for field in fillable:
            keeper_val = keeper.get(field)
            loser_val = loser.get(field)
            # Consider empty string as empty for text fields
            keeper_empty = not keeper_val or (isinstance(keeper_val, str) and not keeper_val.strip())
            loser_has = loser_val and (not isinstance(loser_val, str) or loser_val.strip())
            if keeper_empty and loser_has:
                updates.append(f"{field} = %s")
                update_vals.append(loser_val)

        # 2. Collect additional emails/phones
        existing_emails = keeper.get("additional_emails") or []
        existing_phones = keeper.get("additional_phones") or []

        # Gather loser's email
        loser_email = loser.get("email")
        keeper_email = keeper.get("email")
        if loser_email and loser_email != keeper_email and loser_email not in existing_emails:
            existing_emails.append(loser_email)

        # Gather loser's additional emails
        loser_add_emails = loser.get("additional_emails") or []
        for e in loser_add_emails:
            if e and e != keeper_email and e not in existing_emails:
                existing_emails.append(e)

        # Gather loser's phone
        loser_phone = loser.get("phone")
        keeper_phone = keeper.get("phone")
        if loser_phone and loser_phone != keeper_phone and loser_phone not in existing_phones:
            existing_phones.append(loser_phone)

        # Gather loser's additional phones
        loser_add_phones = loser.get("additional_phones") or []
        for p in loser_add_phones:
            if p and p != keeper_phone and p not in existing_phones:
                existing_phones.append(p)

        if existing_emails:
            updates.append("additional_emails = %s::jsonb")
            update_vals.append(json.dumps(existing_emails))
        if existing_phones:
            updates.append("additional_phones = %s::jsonb")
            update_vals.append(json.dumps(existing_phones))

        # Apply field updates to keeper
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
        cur.execute("""
            INSERT INTO crm_notes (id, title, body, person_id)
            VALUES (%s, %s, %s, %s)
        """, (
            note_id,
            "Duplicate Merged",
            f"Merged duplicate: {loser_name} (id: {loser_id})",
            keeper_id,
        ))

        conn.commit()

        _safe_audit(
            "merge", "person", keeper_id,
            details={
                "loser_id": loser_id,
                "loser_name": loser_name,
                "fields_filled": [u.split(" = ")[0] for u in updates if "=" in u and "updated_at" not in u],
                "emails_collected": existing_emails,
                "phones_collected": existing_phones,
            },
        )

        # Return updated keeper
        return get_person(keeper_id)

    except Exception as e:
        conn.rollback()
        logger.error("Failed to merge person %s into %s: %s", loser_id, keeper_id, e)
        _safe_audit(
            "merge", "person", keeper_id,
            details={"loser_id": loser_id, "error": str(e)},
            status="error",
        )
        return None
    finally:
        conn.close()


def merge_companies(keeper_id: str, loser_id: str) -> dict | None:
    """Merge loser company into keeper.

    1. Fill keeper's empty fields from loser
    2. Re-point crm_people.company_id, crm_notes.company_id
    3. Soft-delete loser
    Returns keeper record on success, None on failure.
    """
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL", (keeper_id,))
        keeper = cur.fetchone()
        cur.execute("SELECT * FROM crm_companies WHERE id = %s AND deleted_at IS NULL", (loser_id,))
        loser = cur.fetchone()

        if not keeper:
            logger.error("merge_companies: keeper %s not found", keeper_id)
            return None
        if not loser:
            logger.warning("merge_companies: loser %s not found or already deleted", loser_id)
            return None

        # 1. Fill keeper's empty fields from loser
        fillable = ["domain_name", "employees", "address_street1", "address_city",
                     "address_state", "linkedin_url"]
        updates = []
        update_vals = []
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

        # 2. Re-point people and notes
        cur.execute(
            "UPDATE crm_people SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
            (keeper_id, loser_id),
        )
        cur.execute(
            "UPDATE crm_notes SET company_id = %s WHERE company_id = %s AND deleted_at IS NULL",
            (keeper_id, loser_id),
        )

        # 3. Soft-delete loser
        cur.execute(
            "UPDATE crm_companies SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s",
            (loser_id,),
        )

        conn.commit()

        loser_name = loser.get("name", "")
        _safe_audit(
            "merge", "company", keeper_id,
            details={
                "loser_id": loser_id,
                "loser_name": loser_name,
                "fields_filled": [u.split(" = ")[0] for u in updates if "=" in u and "updated_at" not in u],
            },
        )

        return get_company(keeper_id)

    except Exception as e:
        conn.rollback()
        logger.error("Failed to merge company %s into %s: %s", loser_id, keeper_id, e)
        _safe_audit(
            "merge", "company", keeper_id,
            details={"loser_id": loser_id, "error": str(e)},
            status="error",
        )
        return None
    finally:
        conn.close()


# ─── Notes ───────────────────────────────────────────────────────────────


def _note_to_dict(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def create_note(title: str, body: str, person_id: str | None = None,
                company_id: str | None = None) -> str | None:
    """Create a note. Returns note UUID."""
    note_id = str(uuid.uuid4())
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO crm_notes (id, title, body, person_id, company_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (note_id, title, body, person_id, company_id))
        conn.commit()
        _safe_audit(
            "create", "note", note_id,
            details={"title": title, "person_id": person_id, "company_id": company_id},
        )
        return note_id
    except Exception as e:
        conn.rollback()
        logger.error("Failed to create note: %s", e)
        return None
    finally:
        conn.close()


def get_note(note_id: str) -> dict | None:
    """Get a note by ID."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM crm_notes WHERE id = %s AND deleted_at IS NULL",
        (note_id,),
    )
    row = cur.fetchone()
    conn.close()
    return _note_to_dict(row) if row else None


def update_note(note_id: str, **fields) -> bool:
    """Update a note. Accepted: title, body, person_id, company_id."""
    col_map = {"title": "title", "body": "body", "person_id": "person_id", "company_id": "company_id"}
    sets = []
    vals = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.append(note_id)
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE crm_notes SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL", vals)
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit(
                "update", "note", note_id,
                details={"fields": list(fields.keys())},
            )
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to update note %s: %s", note_id, e)
        return False
    finally:
        conn.close()


def delete_note(note_id: str) -> bool:
    """Soft-delete a note."""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE crm_notes SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (note_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit("delete", "note", note_id)
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to delete note %s: %s", note_id, e)
        return False
    finally:
        conn.close()


def list_notes(person_id: str | None = None, company_id: str | None = None,
               limit: int = 50) -> list:
    """List notes, optionally filtered by person or company."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if person_id:
        conditions.append("person_id = %s")
        params.append(person_id)
    if company_id:
        conditions.append("company_id = %s")
        params.append(company_id)
    where = " AND ".join(conditions)
    params.append(limit)
    cur.execute(f"SELECT * FROM crm_notes WHERE {where} ORDER BY updated_at DESC LIMIT %s", params)
    rows = cur.fetchall()
    conn.close()
    return [_note_to_dict(r) for r in rows]


# ─── Tasks ───────────────────────────────────────────────────────────────


def _task_to_dict(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "status": row.get("status") or "",
        "dueAt": row["due_at"].isoformat() if row.get("due_at") else None,
        "assigneeId": str(row["assignee_id"]) if row.get("assignee_id") else None,
        "personId": str(row["person_id"]) if row.get("person_id") else None,
        "companyId": str(row["company_id"]) if row.get("company_id") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def create_task(title: str, body: str | None = None, status: str = "TODO",
                due_at: str | None = None, person_id: str | None = None,
                company_id: str | None = None) -> str | None:
    """Create a task. Returns task UUID."""
    task_id = str(uuid.uuid4())
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO crm_tasks (id, title, body, status, due_at, person_id, company_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (task_id, title, body, status, due_at, person_id, company_id))
        conn.commit()
        _safe_audit(
            "create", "task", task_id,
            details={"title": title, "status": status, "person_id": person_id},
        )
        return task_id
    except Exception as e:
        conn.rollback()
        logger.error("Failed to create task: %s", e)
        return None
    finally:
        conn.close()


def get_task(task_id: str) -> dict | None:
    """Get a task by ID."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT * FROM crm_tasks WHERE id = %s AND deleted_at IS NULL",
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return _task_to_dict(row) if row else None


def update_task(task_id: str, **fields) -> bool:
    """Update a task. Accepted: title, body, status, due_at, person_id, company_id."""
    col_map = {
        "title": "title", "body": "body", "status": "status",
        "due_at": "due_at", "person_id": "person_id", "company_id": "company_id",
    }
    sets = []
    vals = []
    for key, col in col_map.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{col} = %s")
            vals.append(fields[key])
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    vals.append(task_id)
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE crm_tasks SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL", vals)
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit(
                "update", "task", task_id,
                details={"fields": list(fields.keys())},
            )
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to update task %s: %s", task_id, e)
        return False
    finally:
        conn.close()


def delete_task(task_id: str) -> bool:
    """Soft-delete a task."""
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE crm_tasks SET deleted_at = NOW(), updated_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (task_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
        if ok:
            _safe_audit("delete", "task", task_id)
        return ok
    except Exception as e:
        conn.rollback()
        logger.error("Failed to delete task %s: %s", task_id, e)
        return False
    finally:
        conn.close()


def list_tasks(status: str | None = None, person_id: str | None = None,
               limit: int = 50) -> list:
    """List tasks, optionally filtered by status or person."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if person_id:
        conditions.append("person_id = %s")
        params.append(person_id)
    where = " AND ".join(conditions)
    params.append(limit)
    cur.execute(f"SELECT * FROM crm_tasks WHERE {where} ORDER BY updated_at DESC LIMIT %s", params)
    rows = cur.fetchall()
    conn.close()
    return [_task_to_dict(r) for r in rows]


# ─── Conversations ───────────────────────────────────────────────────────


def _conversation_to_dict(row: dict) -> dict:
    """Convert a crm_conversations row to API response shape."""
    result = {
        "id": row["id"],
        "status": row.get("status") or "open",
        "inbox_name": row.get("inbox_name") or "",
        "messages_count": row.get("messages_count", 0),
        "last_activity_at": row["last_activity_at"].isoformat() if row.get("last_activity_at") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "additional_attributes": row.get("additional_attributes") or {},
        "custom_attributes": row.get("custom_attributes") or {},
    }
    # Add contact info if joined
    if row.get("contact_name") is not None:
        result["meta"] = {
            "sender": {
                "name": row.get("contact_name") or "",
                "email": row.get("contact_email") or "",
            }
        }
    elif row.get("person_id"):
        result["contact_id"] = str(row["person_id"])
    return result


def list_conversations(status: str = "open", page: int = 1, page_size: int = 25) -> dict:
    """List conversations by status. Returns paginated response shape."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    offset = (page - 1) * page_size

    cur.execute("""
        SELECT c.*, p.first_name || ' ' || p.last_name AS contact_name,
               p.email AS contact_email
        FROM crm_conversations c
        LEFT JOIN crm_people p ON p.id = c.person_id
        WHERE c.status = %s
        ORDER BY c.last_activity_at DESC NULLS LAST
        LIMIT %s OFFSET %s
    """, (status, page_size, offset))
    rows = cur.fetchall()

    # Total count for pagination
    cur.execute("SELECT COUNT(*) FROM crm_conversations WHERE status = %s", (status,))
    total = cur.fetchone()["count"]
    conn.close()

    return {
        "data": {
            "payload": [_conversation_to_dict(r) for r in rows],
            "meta": {
                "all_count": total,
                "page": page,
            },
        }
    }


def get_conversation(conversation_id: int) -> dict | None:
    """Get a single conversation by ID."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.*, p.first_name || ' ' || p.last_name AS contact_name,
               p.email AS contact_email
        FROM crm_conversations c
        LEFT JOIN crm_people p ON p.id = c.person_id
        WHERE c.id = %s
    """, (conversation_id,))
    row = cur.fetchone()
    conn.close()
    return _conversation_to_dict(row) if row else None


def list_messages(conversation_id: int) -> list:
    """List messages in a conversation, ordered by created_at."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT * FROM crm_messages
        WHERE conversation_id = %s
        ORDER BY created_at ASC
    """, (conversation_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "content": r.get("content") or "",
            "message_type": r.get("message_type") or "incoming",
            "private": r.get("private", False),
            "sender": {"name": r.get("sender_name") or "System"},
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in rows
    ]


def send_message(conversation_id: int, content: str,
                 message_type: str = "incoming", private: bool = False,
                 sender_name: str | None = None) -> dict | None:
    """Create a message in a conversation. Updates conversation metadata."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO crm_messages (conversation_id, content, message_type, private, sender_name)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, content, message_type, private, created_at
        """, (conversation_id, content, message_type, private, sender_name))
        msg = cur.fetchone()

        # Update conversation metadata
        cur.execute("""
            UPDATE crm_conversations
            SET messages_count = messages_count + 1,
                last_activity_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
        """, (conversation_id,))

        conn.commit()
        _safe_audit(
            "create", "message", str(msg["id"]),
            details={"conversation_id": conversation_id, "message_type": message_type,
                     "private": private, "content_length": len(content)},
        )
        return {
            "id": msg["id"],
            "content": msg["content"],
            "message_type": msg["message_type"],
            "private": msg["private"],
            "created_at": msg["created_at"].isoformat() if msg.get("created_at") else None,
        }
    except Exception as e:
        conn.rollback()
        logger.error("Failed to send message to conversation %d: %s", conversation_id, e)
        return None
    finally:
        conn.close()


def toggle_conversation_status(conversation_id: int, status: str) -> dict | None:
    """Toggle conversation status (open, resolved, pending, snoozed)."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            UPDATE crm_conversations
            SET status = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING id, status
        """, (status, conversation_id))
        row = cur.fetchone()
        conn.commit()
        if row:
            _safe_audit(
                "update", "conversation", str(row["id"]),
                details={"new_status": status},
            )
            return {"id": row["id"], "current_status": row["status"]}
        return None
    except Exception as e:
        conn.rollback()
        logger.error("Failed to toggle conversation %d status: %s", conversation_id, e)
        return None
    finally:
        conn.close()


def get_conversations_for_contact(person_id: str) -> list:
    """Get all conversations for a person."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT * FROM crm_conversations
        WHERE person_id = %s
        ORDER BY last_activity_at DESC NULLS LAST
    """, (person_id,))
    rows = cur.fetchall()
    conn.close()
    return [_conversation_to_dict(r) for r in rows]


def create_conversation(person_id: str, inbox_name: str = "Robothor Bridge") -> dict | None:
    """Create a new conversation for a person."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO crm_conversations (person_id, status, inbox_name, last_activity_at)
            VALUES (%s, 'open', %s, NOW())
            RETURNING *
        """, (person_id, inbox_name))
        row = cur.fetchone()
        conn.commit()
        if row:
            _safe_audit(
                "create", "conversation", str(row["id"]),
                details={"person_id": person_id, "inbox_name": inbox_name},
            )
        return _conversation_to_dict(row) if row else None
    except Exception as e:
        conn.rollback()
        logger.error("Failed to create conversation: %s", e)
        return None
    finally:
        conn.close()


# ─── Contact Resolution ─────────────────────────────────────────────────


def resolve_contact(channel: str, identifier: str, name: str | None = None) -> dict:
    """Resolve a channel identifier to a person_id. Creates person if needed.

    Replaces contact_resolver.resolve() — no HTTP calls, pure SQL.
    Returns dict with person_id, display_name, and legacy fields for compatibility.
    """
    conn = _conn()
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
        people = search_people(search_term)
        if people:
            person_id = people[0]["id"]
        elif name:
            parts = name.split(None, 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""
            email = identifier if channel == "email" else None
            phone = identifier if channel in ("voice", "sms") else None
            person_id = create_person(first, last, email, phone)

    # Upsert the mapping
    cur.execute("""
        INSERT INTO contact_identifiers (channel, identifier, display_name, person_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (channel, identifier) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, contact_identifiers.display_name),
            person_id = COALESCE(EXCLUDED.person_id, contact_identifiers.person_id),
            updated_at = NOW()
        RETURNING *
    """, (channel, identifier, display, person_id))
    result = cur.fetchone()
    conn.commit()
    conn.close()

    if result:
        audit.log_event(
            "crm.resolve", f"resolve_contact {channel}:{identifier}",
            category="crm",
            target=f"person:{person_id}" if person_id else None,
            details={"channel": channel, "identifier": identifier,
                     "name": name, "person_id": person_id,
                     "existed": existing is not None},
        )

    return dict(result) if result else {}


def get_timeline(identifier: str) -> dict:
    """Get unified timeline for a contact across all systems (pure SQL)."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Find all identifiers for this person
    cur.execute(
        "SELECT * FROM contact_identifiers WHERE identifier = %s OR display_name ILIKE %s",
        (identifier, f"%{identifier}%"),
    )
    mappings = cur.fetchall()

    timeline = {
        "identifier": identifier,
        "mappings": [dict(m) for m in mappings],
        "twenty": None,
        "conversations": [],
        "memory_facts": [],
    }

    if not mappings:
        conn.close()
        return timeline

    # Get person data
    for m in mappings:
        pid = m.get("person_id")
        if pid:
            person = get_person(str(pid))
            if person:
                timeline["person"] = person
                break

    # Get conversations
    for m in mappings:
        pid = m.get("person_id")
        if pid:
            convos = get_conversations_for_contact(str(pid))
            timeline["conversations"] = convos
            break

    # Get memory facts (still uses orchestrator, but lightweight)
    try:
        import httpx
        r = httpx.get(f"{config.MEMORY_URL}/search", params={"query": identifier, "limit": 10}, timeout=5.0)
        if r.status_code == 200:
            timeline["memory_facts"] = r.json().get("results", [])
    except Exception:
        pass

    conn.close()
    return timeline


# ─── Metadata Introspection ─────────────────────────────────────────────


def get_metadata_objects() -> list:
    """Return list of CRM table names (for MCP get_metadata_objects tool)."""
    return [
        {"name": "crm_people", "label": "People"},
        {"name": "crm_companies", "label": "Companies"},
        {"name": "crm_notes", "label": "Notes"},
        {"name": "crm_tasks", "label": "Tasks"},
        {"name": "crm_conversations", "label": "Conversations"},
        {"name": "crm_messages", "label": "Messages"},
    ]


def get_object_metadata(object_name: str) -> dict | None:
    """Return column info for a CRM table (for MCP get_object_metadata tool)."""
    valid = {"crm_people", "crm_companies", "crm_notes", "crm_tasks",
             "crm_conversations", "crm_messages"}
    if object_name not in valid:
        return None

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (object_name,))
    cols = cur.fetchall()
    conn.close()
    return {"name": object_name, "columns": [dict(c) for c in cols]}


def search_records(query: str, object_name: str | None = None, limit: int = 20) -> list:
    """Cross-table search (for MCP search_records tool)."""
    results = []
    pattern = f"%{query}%"

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    tables_to_search = []
    if object_name:
        tables_to_search = [object_name]
    else:
        tables_to_search = ["crm_people", "crm_companies", "crm_notes", "crm_tasks"]

    for table in tables_to_search:
        if table == "crm_people":
            cur.execute("""
                SELECT id, first_name || ' ' || last_name AS label, 'person' AS type
                FROM crm_people
                WHERE deleted_at IS NULL
                  AND (first_name ILIKE %s OR last_name ILIKE %s OR email ILIKE %s)
                LIMIT %s
            """, (pattern, pattern, pattern, limit))
        elif table == "crm_companies":
            cur.execute("""
                SELECT id, name AS label, 'company' AS type
                FROM crm_companies
                WHERE deleted_at IS NULL AND name ILIKE %s
                LIMIT %s
            """, (pattern, limit))
        elif table == "crm_notes":
            cur.execute("""
                SELECT id, title AS label, 'note' AS type
                FROM crm_notes
                WHERE deleted_at IS NULL AND (title ILIKE %s OR body ILIKE %s)
                LIMIT %s
            """, (pattern, pattern, limit))
        elif table == "crm_tasks":
            cur.execute("""
                SELECT id, title AS label, 'task' AS type
                FROM crm_tasks
                WHERE deleted_at IS NULL AND (title ILIKE %s OR body ILIKE %s)
                LIMIT %s
            """, (pattern, pattern, limit))
        else:
            continue

        for row in cur.fetchall():
            results.append({"id": str(row["id"]), "label": row["label"], "type": row["type"]})

    conn.close()
    return results[:limit]


# ─── CRM Health Check ───────────────────────────────────────────────────


def check_health() -> dict:
    """Quick health check — verifies crm_* tables are accessible."""
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM crm_people WHERE deleted_at IS NULL")
        people_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM crm_conversations")
        conv_count = cur.fetchone()[0]
        conn.close()
        return {"status": "ok", "people": people_count, "conversations": conv_count}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── CRM Fetcher Support ────────────────────────────────────────────────
# These functions support crm_fetcher.py with the same return shapes.


def fetch_conversations_for_ingestion(hours: int = 24) -> list:
    """Fetch recent conversations with messages for pipeline ingestion.

    Returns same shape as crm_fetcher.fetch_conversations().
    """
    from datetime import timedelta
    cutoff = _now() - timedelta(hours=hours)

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.id, c.status, c.inbox_name, c.last_activity_at,
               p.first_name || ' ' || COALESCE(p.last_name, '') AS contact_name,
               p.email AS contact_email
        FROM crm_conversations c
        LEFT JOIN crm_people p ON p.id = c.person_id
        WHERE c.last_activity_at >= %s
        ORDER BY c.last_activity_at DESC
    """, (cutoff,))
    convos = cur.fetchall()

    results = []
    for conv in convos:
        cur.execute("""
            SELECT sender_name, content, created_at, message_type, private
            FROM crm_messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
        """, (conv["id"],))
        messages = [
            {
                "sender": r["sender_name"] or "System",
                "content": r["content"] or "",
                "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                "type": 1 if r["message_type"] == "outgoing" else 0,
                "private": r["private"],
            }
            for r in cur.fetchall()
        ]
        results.append({
            "id": conv["id"],
            "contact_name": conv["contact_name"] or "Unknown",
            "contact_email": conv["contact_email"] or "",
            "status": conv["status"],
            "inbox_name": conv["inbox_name"],
            "messages": messages,
            "last_activity_at": conv["last_activity_at"].timestamp() if conv["last_activity_at"] else None,
        })

    conn.close()
    return results


def fetch_contacts_for_ingestion(hours: int = 24) -> list:
    """Fetch recently updated people for pipeline ingestion.

    Returns same shape as crm_fetcher.fetch_twenty_contacts().
    """
    from datetime import timedelta
    cutoff = _now() - timedelta(hours=hours)

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT p.*, c.name AS company_name
        FROM crm_people p
        LEFT JOIN crm_companies c ON c.id = p.company_id
        WHERE p.deleted_at IS NULL AND p.updated_at >= %s
        ORDER BY p.updated_at DESC
        LIMIT 100
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": str(r["id"]),
            "firstName": r.get("first_name") or "",
            "lastName": r.get("last_name") or "",
            "email": r.get("email") or "",
            "phone": r.get("phone") or "",
            "jobTitle": r.get("job_title") or "",
            "company": r.get("company_name") or "",
            "city": r.get("city") or "",
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in rows
    ]


def fetch_all_contacts_for_ingestion() -> list:
    """Fetch all people for pipeline ingestion. Same shape as fetch_contacts_for_ingestion."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT p.*, c.name AS company_name
        FROM crm_people p
        LEFT JOIN crm_companies c ON c.id = p.company_id
        WHERE p.deleted_at IS NULL
        ORDER BY p.updated_at DESC
        LIMIT 500
    """)
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": str(r["id"]),
            "firstName": r.get("first_name") or "",
            "lastName": r.get("last_name") or "",
            "email": r.get("email") or "",
            "phone": r.get("phone") or "",
            "jobTitle": r.get("job_title") or "",
            "company": r.get("company_name") or "",
            "city": r.get("city") or "",
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in rows
    ]


def fetch_notes_for_ingestion(hours: int = 24) -> list:
    """Fetch recent notes for pipeline ingestion.

    Returns same shape as crm_fetcher.fetch_twenty_notes().
    """
    from datetime import timedelta
    cutoff = _now() - timedelta(hours=hours)

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT n.*, p.first_name, p.last_name, c.name AS company_name
        FROM crm_notes n
        LEFT JOIN crm_people p ON p.id = n.person_id
        LEFT JOIN crm_companies c ON c.id = n.company_id
        WHERE n.deleted_at IS NULL AND n.updated_at >= %s
        ORDER BY n.updated_at DESC
        LIMIT 50
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()

    results = []
    for r in rows:
        targets = []
        if r.get("first_name"):
            targets.append(f"{r['first_name']} {r.get('last_name', '')}".strip())
        if r.get("company_name"):
            targets.append(f"Company: {r['company_name']}")
        results.append({
            "id": str(r["id"]),
            "title": r.get("title") or "",
            "body": r.get("body") or "",
            "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            "targets": targets,
        })
    return results


def fetch_tasks_for_ingestion(hours: int = 24) -> list:
    """Fetch recent tasks for pipeline ingestion.

    Returns same shape as crm_fetcher.fetch_twenty_tasks().
    """
    from datetime import timedelta
    cutoff = _now() - timedelta(hours=hours)

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT t.*, p.first_name, p.last_name
        FROM crm_tasks t
        LEFT JOIN crm_people p ON p.id = t.person_id
        WHERE t.deleted_at IS NULL AND t.updated_at >= %s
        ORDER BY t.updated_at DESC
        LIMIT 50
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()

    results = []
    for r in rows:
        targets = []
        if r.get("first_name"):
            targets.append(f"{r['first_name']} {r.get('last_name', '')}".strip())
        results.append({
            "id": str(r["id"]),
            "title": r.get("title") or "",
            "body": r.get("body") or "",
            "status": r.get("status") or "",
            "dueAt": r["due_at"].isoformat() if r.get("due_at") else None,
            "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            "targets": targets,
        })
    return results
