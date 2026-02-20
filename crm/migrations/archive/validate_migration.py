#!/usr/bin/env python3
"""
CRM Migration Validator — compares old (HTTP) vs new (SQL) data paths.

Checks that crm_* tables contain the same data as the source
Twenty and Chatwoot databases. Also validates contact resolution
consistency and response format compatibility.

Usage:
    python validate_migration.py
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TWENTY_DSN = "dbname=twenty_crm user=philip host=/var/run/postgresql"
CHATWOOT_DSN = "dbname=chatwoot user=philip host=/var/run/postgresql"
MEMORY_DSN = "dbname=robothor_memory user=philip host=/var/run/postgresql"

TWENTY_SCHEMA = "workspace_805x8k0xffroz7305zozbuntr"


def validate_row_counts():
    """Compare row counts between source and target tables."""
    issues = []

    twenty = psycopg2.connect(TWENTY_DSN)
    chatwoot = psycopg2.connect(CHATWOOT_DSN)
    memory = psycopg2.connect(MEMORY_DSN)

    checks = [
        ("companies", f'SELECT COUNT(*) FROM {TWENTY_SCHEMA}.company WHERE "deletedAt" IS NULL', twenty,
         "SELECT COUNT(*) FROM crm_companies WHERE deleted_at IS NULL", memory),
        ("people", f'SELECT COUNT(*) FROM {TWENTY_SCHEMA}.person WHERE "deletedAt" IS NULL', twenty,
         "SELECT COUNT(*) FROM crm_people WHERE deleted_at IS NULL", memory),
        ("notes", f'SELECT COUNT(*) FROM {TWENTY_SCHEMA}.note WHERE "deletedAt" IS NULL', twenty,
         "SELECT COUNT(*) FROM crm_notes WHERE deleted_at IS NULL", memory),
        ("tasks", f'SELECT COUNT(*) FROM {TWENTY_SCHEMA}.task WHERE "deletedAt" IS NULL', twenty,
         "SELECT COUNT(*) FROM crm_tasks WHERE deleted_at IS NULL", memory),
        ("conversations", "SELECT COUNT(*) FROM conversations", chatwoot,
         "SELECT COUNT(*) FROM crm_conversations", memory),
        ("messages", "SELECT COUNT(*) FROM messages", chatwoot,
         "SELECT COUNT(*) FROM crm_messages", memory),
    ]

    logger.info("=== Row Count Validation ===")
    for name, src_sql, src_conn, dst_sql, dst_conn in checks:
        src_cur = src_conn.cursor()
        src_cur.execute(src_sql)
        src_count = src_cur.fetchone()[0]

        dst_cur = dst_conn.cursor()
        dst_cur.execute(dst_sql)
        dst_count = dst_cur.fetchone()[0]

        status = "OK" if src_count == dst_count else "MISMATCH"
        if status == "MISMATCH":
            issues.append(f"{name}: source={src_count}, target={dst_count}")
        logger.info("  %s: source=%d, target=%d [%s]", name, src_count, dst_count, status)

    twenty.close()
    chatwoot.close()
    memory.close()
    return issues


def validate_people_data():
    """Spot-check that people data matches between Twenty and crm_people."""
    issues = []
    twenty = psycopg2.connect(TWENTY_DSN)
    memory = psycopg2.connect(MEMORY_DSN)

    tcur = twenty.cursor(cursor_factory=RealDictCursor)
    tcur.execute(f"""
        SELECT id, "nameFirstName", "nameLastName", "emailsPrimaryEmail",
               "phonesPrimaryPhoneNumber", "jobTitle", "companyId"
        FROM {TWENTY_SCHEMA}.person
        WHERE "deletedAt" IS NULL
        ORDER BY "createdAt"
        LIMIT 20
    """)

    mcur = memory.cursor(cursor_factory=RealDictCursor)

    logger.info("=== People Data Validation (first 20) ===")
    for row in tcur.fetchall():
        mcur.execute("SELECT * FROM crm_people WHERE id = %s", (row["id"],))
        crm_row = mcur.fetchone()

        if not crm_row:
            issues.append(f"Person {row['id']} missing in crm_people")
            logger.warning("  MISSING: %s %s (id=%s)", row["nameFirstName"], row["nameLastName"], row["id"])
            continue

        mismatches = []
        if (crm_row["first_name"] or "") != (row["nameFirstName"] or ""):
            mismatches.append(f"first_name: {crm_row['first_name']!r} vs {row['nameFirstName']!r}")
        if (crm_row["last_name"] or "") != (row["nameLastName"] or ""):
            mismatches.append(f"last_name: {crm_row['last_name']!r} vs {row['nameLastName']!r}")
        if (crm_row["email"] or "") != (row["emailsPrimaryEmail"] or ""):
            mismatches.append(f"email: {crm_row['email']!r} vs {row['emailsPrimaryEmail']!r}")

        if mismatches:
            issues.append(f"Person {row['id']}: {'; '.join(mismatches)}")
            logger.warning("  MISMATCH %s: %s", row["id"], "; ".join(mismatches))
        else:
            logger.info("  OK: %s %s", row["nameFirstName"], row["nameLastName"])

    twenty.close()
    memory.close()
    return issues


def validate_conversations_data():
    """Spot-check conversation data between Chatwoot and crm_conversations."""
    issues = []
    chatwoot = psycopg2.connect(CHATWOOT_DSN)
    memory = psycopg2.connect(MEMORY_DSN)

    ccur = chatwoot.cursor(cursor_factory=RealDictCursor)
    ccur.execute("SELECT id, status, contact_id FROM conversations ORDER BY id LIMIT 20")

    mcur = memory.cursor(cursor_factory=RealDictCursor)

    status_map = {0: "open", 1: "resolved", 2: "pending", 3: "snoozed"}

    logger.info("=== Conversation Data Validation (first 20) ===")
    for row in ccur.fetchall():
        mcur.execute("SELECT * FROM crm_conversations WHERE id = %s", (row["id"],))
        crm_row = mcur.fetchone()

        if not crm_row:
            issues.append(f"Conversation {row['id']} missing in crm_conversations")
            logger.warning("  MISSING: conversation id=%d", row["id"])
            continue

        expected_status = status_map.get(row["status"], "open")
        if crm_row["status"] != expected_status:
            issues.append(f"Conversation {row['id']}: status {crm_row['status']!r} vs {expected_status!r}")
            logger.warning("  MISMATCH conv %d: status %s vs %s", row["id"], crm_row["status"], expected_status)
        else:
            logger.info("  OK: conversation %d (status=%s)", row["id"], crm_row["status"])

    chatwoot.close()
    memory.close()
    return issues


def validate_message_counts():
    """Check that message counts per conversation match."""
    issues = []
    chatwoot = psycopg2.connect(CHATWOOT_DSN)
    memory = psycopg2.connect(MEMORY_DSN)

    ccur = chatwoot.cursor(cursor_factory=RealDictCursor)
    ccur.execute("""
        SELECT conversation_id, COUNT(*) as cnt
        FROM messages
        GROUP BY conversation_id
        ORDER BY conversation_id
    """)

    mcur = memory.cursor(cursor_factory=RealDictCursor)

    logger.info("=== Message Count Validation ===")
    mismatched = 0
    total = 0
    for row in ccur.fetchall():
        total += 1
        mcur.execute(
            "SELECT COUNT(*) as cnt FROM crm_messages WHERE conversation_id = %s",
            (row["conversation_id"],),
        )
        crm_count = mcur.fetchone()["cnt"]

        if crm_count != row["cnt"]:
            mismatched += 1
            issues.append(f"Conv {row['conversation_id']}: {crm_count} vs {row['cnt']} messages")
            if mismatched <= 5:
                logger.warning("  MISMATCH conv %d: %d vs %d messages",
                              row["conversation_id"], crm_count, row["cnt"])

    if mismatched > 5:
        logger.warning("  ... and %d more mismatched conversations", mismatched - 5)
    logger.info("  %d/%d conversations match message counts", total - mismatched, total)

    chatwoot.close()
    memory.close()
    return issues


def validate_contact_identifiers():
    """Check that person_id backfill worked correctly."""
    issues = []
    memory = psycopg2.connect(MEMORY_DSN)
    cur = memory.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT id, twenty_person_id, person_id
        FROM contact_identifiers
        WHERE twenty_person_id IS NOT NULL
    """)
    rows = cur.fetchall()

    logger.info("=== Contact Identifiers Validation ===")
    mismatched = 0
    for row in rows:
        if row["person_id"] is None:
            mismatched += 1
            issues.append(f"CI {row['id']}: person_id is NULL but twenty_person_id is {row['twenty_person_id']}")
        elif str(row["person_id"]) != str(row["twenty_person_id"]):
            mismatched += 1
            issues.append(f"CI {row['id']}: person_id {row['person_id']} != twenty_person_id {row['twenty_person_id']}")

    logger.info("  %d/%d contact_identifiers have correct person_id backfill",
                len(rows) - mismatched, len(rows))
    memory.close()
    return issues


def validate_crm_dal_response_formats():
    """Test that crm_dal functions return expected response shapes."""
    issues = []
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

    try:
        import crm_dal
    except ImportError as e:
        issues.append(f"Cannot import crm_dal: {e}")
        logger.error("Cannot import crm_dal: %s", e)
        return issues

    logger.info("=== DAL Response Format Validation ===")

    # list_people
    people = crm_dal.list_people(limit=3)
    if people:
        p = people[0]
        required_keys = {"id", "name", "emails", "phones", "jobTitle"}
        missing = required_keys - set(p.keys())
        if missing:
            issues.append(f"list_people missing keys: {missing}")
            logger.warning("  list_people missing keys: %s", missing)
        elif not isinstance(p["name"], dict) or "firstName" not in p["name"]:
            issues.append("list_people: name is not {firstName, lastName} dict")
            logger.warning("  list_people: name format wrong: %s", type(p["name"]))
        else:
            logger.info("  list_people: OK (shape verified, %d results)", len(people))
    else:
        logger.info("  list_people: OK (empty, no data to validate)")

    # list_conversations
    convos = crm_dal.list_conversations("open")
    if "data" in convos and "payload" in convos["data"]:
        logger.info("  list_conversations: OK (shape verified)")
    else:
        issues.append("list_conversations: missing data.payload structure")
        logger.warning("  list_conversations: wrong shape: %s", list(convos.keys()))

    # check_health
    health = crm_dal.check_health()
    if health.get("status") == "ok":
        logger.info("  check_health: OK (people=%d, conversations=%d)",
                    health["people"], health["conversations"])
    else:
        issues.append(f"check_health: {health}")
        logger.warning("  check_health: %s", health)

    return issues


def main():
    logger.info("=" * 60)
    logger.info("CRM Migration Validation — %s", datetime.now(timezone.utc).isoformat())
    logger.info("=" * 60)

    all_issues = []
    all_issues.extend(validate_row_counts())
    all_issues.extend(validate_people_data())
    all_issues.extend(validate_conversations_data())
    all_issues.extend(validate_message_counts())
    all_issues.extend(validate_contact_identifiers())
    all_issues.extend(validate_crm_dal_response_formats())

    logger.info("")
    logger.info("=" * 60)
    if all_issues:
        logger.warning("VALIDATION FOUND %d ISSUES:", len(all_issues))
        for issue in all_issues:
            logger.warning("  - %s", issue)
    else:
        logger.info("VALIDATION PASSED — all checks OK")
    logger.info("=" * 60)

    # Write report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issues_count": len(all_issues),
        "issues": all_issues,
        "status": "PASS" if not all_issues else "FAIL",
    }
    report_path = Path(__file__).parent / "validation_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report written to %s", report_path)

    return 0 if not all_issues else 1


if __name__ == "__main__":
    sys.exit(main())
