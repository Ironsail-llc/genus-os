#!/usr/bin/env python3
"""
CRM Data Migration: Twenty CRM + Chatwoot → native crm_* tables.

Reads directly from twenty_crm and chatwoot PostgreSQL databases,
transforms field names, and inserts into crm_* tables in robothor_memory.

Old databases remain untouched — rollback is always possible.

Usage:
    python consolidate_data.py              # dry run (report only)
    python consolidate_data.py --execute    # actually migrate
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Database connection strings
TWENTY_DSN = "dbname=twenty_crm user=philip host=/var/run/postgresql"
CHATWOOT_DSN = "dbname=chatwoot user=philip host=/var/run/postgresql"
MEMORY_DSN = "dbname=robothor_memory user=philip host=/var/run/postgresql"

# Twenty workspace schema (discovered from DB inspection)
TWENTY_SCHEMA = "workspace_805x8k0xffroz7305zozbuntr"

# Chatwoot status int→string mapping
CHATWOOT_STATUS_MAP = {0: "open", 1: "resolved", 2: "pending", 3: "snoozed"}
CHATWOOT_MSG_TYPE_MAP = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}


def migrate_companies(twenty_conn, memory_conn, execute: bool) -> int:
    """Migrate companies from Twenty to crm_companies."""
    cur = twenty_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        SELECT id, name, "domainNamePrimaryLinkUrl", employees,
               "addressAddressStreet1", "addressAddressStreet2",
               "addressAddressCity", "addressAddressState",
               "addressAddressPostcode", "addressAddressCountry",
               "linkedinLinkPrimaryLinkUrl",
               "annualRecurringRevenueAmountMicros",
               "annualRecurringRevenueCurrencyCode",
               "idealCustomerProfile",
               "createdAt", "updatedAt", "deletedAt"
        FROM {TWENTY_SCHEMA}.company
        WHERE "deletedAt" IS NULL
        ORDER BY "createdAt"
    """)
    rows = cur.fetchall()
    logger.info("Found %d companies in Twenty", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()
    for r in rows:
        wcur.execute("""
            INSERT INTO crm_companies (
                id, name, domain_name, employees,
                address_street1, address_street2, address_city,
                address_state, address_postcode, address_country,
                linkedin_url,
                annual_recurring_revenue_micros, annual_recurring_revenue_currency,
                ideal_customer_profile,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], r["name"], r["domainNamePrimaryLinkUrl"],
            int(r["employees"]) if r["employees"] else None,
            r["addressAddressStreet1"], r["addressAddressStreet2"],
            r["addressAddressCity"], r["addressAddressState"],
            r["addressAddressPostcode"], r["addressAddressCountry"],
            r.get("linkedinLinkPrimaryLinkUrl"),
            int(r["annualRecurringRevenueAmountMicros"]) if r.get("annualRecurringRevenueAmountMicros") else None,
            r.get("annualRecurringRevenueCurrencyCode"),
            r["idealCustomerProfile"],
            r["createdAt"], r["updatedAt"],
        ))
    memory_conn.commit()
    return len(rows)


def migrate_people(twenty_conn, memory_conn, execute: bool) -> int:
    """Migrate people from Twenty to crm_people."""
    cur = twenty_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        SELECT id, "nameFirstName", "nameLastName",
               "emailsPrimaryEmail", "emailsAdditionalEmails",
               "phonesPrimaryPhoneNumber", "phonesPrimaryPhoneCountryCode",
               "phonesPrimaryPhoneCallingCode", "phonesAdditionalPhones",
               "jobTitle", city, "avatarUrl",
               "linkedinLinkPrimaryLinkUrl", "xLinkPrimaryLinkUrl",
               "companyId",
               "createdAt", "updatedAt", "deletedAt"
        FROM {TWENTY_SCHEMA}.person
        WHERE "deletedAt" IS NULL
        ORDER BY "createdAt"
    """)
    rows = cur.fetchall()
    logger.info("Found %d people in Twenty", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()
    for r in rows:
        wcur.execute("""
            INSERT INTO crm_people (
                id, first_name, last_name, email, additional_emails,
                phone, phone_country_code, phone_calling_code, additional_phones,
                job_title, city, avatar_url, linkedin_url, x_url,
                company_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], r["nameFirstName"], r["nameLastName"],
            r["emailsPrimaryEmail"],
            json.dumps(r["emailsAdditionalEmails"]) if r.get("emailsAdditionalEmails") else None,
            r["phonesPrimaryPhoneNumber"], r.get("phonesPrimaryPhoneCountryCode"),
            r.get("phonesPrimaryPhoneCallingCode"),
            json.dumps(r["phonesAdditionalPhones"]) if r.get("phonesAdditionalPhones") else None,
            r["jobTitle"] or "", r["city"] or "", r["avatarUrl"] or "",
            r.get("linkedinLinkPrimaryLinkUrl"), r.get("xLinkPrimaryLinkUrl"),
            r["companyId"],
            r["createdAt"], r["updatedAt"],
        ))
    memory_conn.commit()
    return len(rows)


def migrate_notes(twenty_conn, memory_conn, execute: bool) -> int:
    """Migrate notes from Twenty to crm_notes (with targets)."""
    cur = twenty_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        SELECT n.id, n.title, n.body, n."createdAt", n."updatedAt",
               nt."personId", nt."companyId"
        FROM {TWENTY_SCHEMA}.note n
        LEFT JOIN {TWENTY_SCHEMA}."noteTarget" nt ON nt."noteId" = n.id AND nt."deletedAt" IS NULL
        WHERE n."deletedAt" IS NULL
        ORDER BY n."createdAt"
    """)
    rows = cur.fetchall()
    logger.info("Found %d note rows in Twenty (including targets)", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()
    for r in rows:
        wcur.execute("""
            INSERT INTO crm_notes (id, title, body, person_id, company_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], r["title"], r["body"],
            r["personId"], r["companyId"],
            r["createdAt"], r["updatedAt"],
        ))
    memory_conn.commit()
    return len(rows)


def migrate_tasks(twenty_conn, memory_conn, execute: bool) -> int:
    """Migrate tasks from Twenty to crm_tasks (with targets)."""
    cur = twenty_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        SELECT t.id, t.title, t.body, t.status, t."dueAt", t."assigneeId",
               t."createdAt", t."updatedAt",
               tt."personId", tt."companyId"
        FROM {TWENTY_SCHEMA}.task t
        LEFT JOIN {TWENTY_SCHEMA}."taskTarget" tt ON tt."taskId" = t.id AND tt."deletedAt" IS NULL
        WHERE t."deletedAt" IS NULL
        ORDER BY t."createdAt"
    """)
    rows = cur.fetchall()
    logger.info("Found %d task rows in Twenty (including targets)", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()
    for r in rows:
        wcur.execute("""
            INSERT INTO crm_tasks (
                id, title, body, status, due_at, assignee_id,
                person_id, company_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], r["title"], r["body"], r["status"], r["dueAt"],
            r["assigneeId"], r["personId"], r["companyId"],
            r["createdAt"], r["updatedAt"],
        ))
    memory_conn.commit()
    return len(rows)


def _build_chatwoot_contact_to_person_map(chatwoot_conn, memory_conn) -> dict:
    """Build mapping from Chatwoot contact_id → crm_people.id via contact_identifiers."""
    # Get all contact_identifiers that have both chatwoot_contact_id and twenty_person_id
    mcur = memory_conn.cursor(cursor_factory=RealDictCursor)
    mcur.execute("""
        SELECT DISTINCT chatwoot_contact_id, twenty_person_id
        FROM contact_identifiers
        WHERE chatwoot_contact_id IS NOT NULL AND twenty_person_id IS NOT NULL
    """)
    ci_map = {str(r["chatwoot_contact_id"]): r["twenty_person_id"] for r in mcur.fetchall()}

    # Also try matching by name/email between chatwoot contacts and crm_people
    ccur = chatwoot_conn.cursor(cursor_factory=RealDictCursor)
    ccur.execute("SELECT id, name, email FROM contacts WHERE name IS NOT NULL AND name != ''")
    chatwoot_contacts = ccur.fetchall()

    pcur = memory_conn.cursor(cursor_factory=RealDictCursor)
    pcur.execute("SELECT id, first_name, last_name, email FROM crm_people")
    people = pcur.fetchall()

    # Build email→person_id and name→person_id indexes
    email_idx = {p["email"].lower(): str(p["id"]) for p in people if p.get("email")}
    name_idx = {}
    for p in people:
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip().lower()
        if full:
            name_idx[full] = str(p["id"])

    for cc in chatwoot_contacts:
        cid = str(cc["id"])
        if cid in ci_map:
            continue
        # Try email match
        if cc.get("email") and cc["email"].lower() in email_idx:
            ci_map[cid] = email_idx[cc["email"].lower()]
            continue
        # Try name match
        cname = (cc.get("name") or "").strip().lower()
        if cname and cname in name_idx:
            ci_map[cid] = name_idx[cname]

    return ci_map


def migrate_conversations(chatwoot_conn, memory_conn, execute: bool) -> int:
    """Migrate conversations from Chatwoot to crm_conversations."""
    contact_map = _build_chatwoot_contact_to_person_map(chatwoot_conn, memory_conn) if execute else {}

    cur = chatwoot_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.id, c.contact_id, c.status, c.additional_attributes,
               c.custom_attributes, c.created_at, c.updated_at,
               i.name as inbox_name
        FROM conversations c
        LEFT JOIN inboxes i ON i.id = c.inbox_id
        ORDER BY c.id
    """)
    rows = cur.fetchall()
    logger.info("Found %d conversations in Chatwoot", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()

    # Get message counts for each conversation
    cur.execute("""
        SELECT conversation_id, COUNT(*) as cnt,
               MAX(created_at) as last_msg
        FROM messages
        GROUP BY conversation_id
    """)
    msg_stats = {r["conversation_id"]: (r["cnt"], r["last_msg"]) for r in cur.fetchall()}

    # Get set of valid person IDs in crm_people (for FK safety)
    mcur = memory_conn.cursor(cursor_factory=RealDictCursor)
    mcur.execute("SELECT id FROM crm_people")
    valid_person_ids = {str(r["id"]) for r in mcur.fetchall()}

    for r in rows:
        person_id = contact_map.get(str(r["contact_id"]))
        # Null out person_id if it references a deleted/missing person
        if person_id and str(person_id) not in valid_person_ids:
            logger.warning("Conversation %d: person_id %s not in crm_people, setting NULL",
                          r["id"], person_id)
            person_id = None
        status_str = CHATWOOT_STATUS_MAP.get(r["status"], "open")
        stats = msg_stats.get(r["id"], (0, None))

        # Use SET val for the SERIAL sequence to preserve Chatwoot IDs
        wcur.execute("""
            INSERT INTO crm_conversations (
                id, person_id, status, inbox_name,
                messages_count, last_activity_at,
                additional_attributes, custom_attributes,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], person_id, status_str,
            r.get("inbox_name") or "",
            stats[0], stats[1],
            json.dumps(r["additional_attributes"]) if r.get("additional_attributes") else None,
            json.dumps(r["custom_attributes"]) if r.get("custom_attributes") else None,
            r["created_at"], r["updated_at"],
        ))

    # Advance the sequence past the max id
    wcur.execute("SELECT COALESCE(MAX(id), 0) FROM crm_conversations")
    max_id = wcur.fetchone()[0]
    wcur.execute(f"SELECT setval('crm_conversations_id_seq', %s, true)", (max_id,))

    memory_conn.commit()
    return len(rows)


def migrate_messages(chatwoot_conn, memory_conn, execute: bool) -> int:
    """Migrate messages from Chatwoot to crm_messages."""
    cur = chatwoot_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT m.id, m.conversation_id, m.content, m.message_type,
               m.private, m.content_type,
               m.sender_type, m.sender_id,
               m.created_at, m.updated_at
        FROM messages m
        ORDER BY m.id
    """)
    rows = cur.fetchall()
    logger.info("Found %d messages in Chatwoot", len(rows))

    if not execute:
        return len(rows)

    # Get sender names from contacts and users
    cur.execute("SELECT id, name FROM contacts")
    contact_names = {r["id"]: r["name"] for r in cur.fetchall()}
    cur.execute("SELECT id, name FROM users")
    user_names = {r["id"]: r["name"] for r in cur.fetchall()}

    # Get set of valid conversation IDs in our new table
    mcur = memory_conn.cursor()
    mcur.execute("SELECT id FROM crm_conversations")
    valid_conv_ids = {r[0] for r in mcur.fetchall()}

    wcur = memory_conn.cursor()
    skipped = 0
    for r in rows:
        if r["conversation_id"] not in valid_conv_ids:
            skipped += 1
            continue

        # Resolve sender name
        sender_name = None
        if r["sender_type"] == "Contact" and r["sender_id"]:
            sender_name = contact_names.get(r["sender_id"])
        elif r["sender_type"] == "User" and r["sender_id"]:
            sender_name = user_names.get(r["sender_id"])

        msg_type = CHATWOOT_MSG_TYPE_MAP.get(r["message_type"], "incoming")

        wcur.execute("""
            INSERT INTO crm_messages (
                id, conversation_id, content, message_type, private,
                sender_name, sender_type, content_type,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            r["id"], r["conversation_id"], r["content"], msg_type,
            r["private"] or False,
            sender_name, r["sender_type"],
            str(r["content_type"]) if r.get("content_type") is not None else None,
            r["created_at"], r["updated_at"],
        ))

    # Advance the sequence
    wcur.execute("SELECT COALESCE(MAX(id), 0) FROM crm_messages")
    max_id = wcur.fetchone()[0]
    wcur.execute(f"SELECT setval('crm_messages_id_seq', %s, true)", (max_id,))

    memory_conn.commit()
    if skipped:
        logger.warning("Skipped %d messages with missing conversation_id", skipped)
    return len(rows) - skipped


def backfill_contact_identifiers(memory_conn, execute: bool) -> int:
    """Backfill person_id in contact_identifiers from twenty_person_id."""
    cur = memory_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, twenty_person_id FROM contact_identifiers
        WHERE twenty_person_id IS NOT NULL AND (person_id IS NULL OR person_id::text != twenty_person_id)
    """)
    rows = cur.fetchall()
    logger.info("Found %d contact_identifiers to backfill person_id", len(rows))

    if not execute:
        return len(rows)

    wcur = memory_conn.cursor()
    for r in rows:
        try:
            wcur.execute(
                "UPDATE contact_identifiers SET person_id = %s::uuid WHERE id = %s",
                (r["twenty_person_id"], r["id"])
            )
        except Exception as e:
            logger.warning("Failed to backfill row %s: %s", r["id"], e)
            memory_conn.rollback()
            continue

    memory_conn.commit()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="CRM data migration: Twenty + Chatwoot → native tables")
    parser.add_argument("--execute", action="store_true", help="Actually run the migration (default: dry run)")
    args = parser.parse_args()

    execute = args.execute
    mode = "EXECUTE" if execute else "DRY RUN"
    logger.info("=== CRM Data Migration [%s] ===", mode)

    report = {}

    try:
        twenty_conn = psycopg2.connect(TWENTY_DSN)
        chatwoot_conn = psycopg2.connect(CHATWOOT_DSN)
        memory_conn = psycopg2.connect(MEMORY_DSN)
    except Exception as e:
        logger.error("Failed to connect to databases: %s", e)
        sys.exit(1)

    try:
        # Phase 1: Companies (must be first — people reference companies)
        report["companies"] = migrate_companies(twenty_conn, memory_conn, execute)

        # Phase 2: People
        report["people"] = migrate_people(twenty_conn, memory_conn, execute)

        # Phase 3: Notes
        report["notes"] = migrate_notes(twenty_conn, memory_conn, execute)

        # Phase 4: Tasks
        report["tasks"] = migrate_tasks(twenty_conn, memory_conn, execute)

        # Phase 5: Conversations (needs person mapping)
        report["conversations"] = migrate_conversations(chatwoot_conn, memory_conn, execute)

        # Phase 6: Messages
        report["messages"] = migrate_messages(chatwoot_conn, memory_conn, execute)

        # Phase 7: Backfill contact_identifiers.person_id
        report["contact_identifiers_backfilled"] = backfill_contact_identifiers(memory_conn, execute)

    except Exception as e:
        logger.error("Migration failed: %s", e)
        raise
    finally:
        twenty_conn.close()
        chatwoot_conn.close()
        memory_conn.close()

    # Write report
    report_path = Path(__file__).parent / "migration_report.json"
    report["mode"] = mode
    report["timestamp"] = datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report written to %s", report_path)

    logger.info("=== Migration Report ===")
    for k, v in report.items():
        logger.info("  %s: %s", k, v)


if __name__ == "__main__":
    main()
