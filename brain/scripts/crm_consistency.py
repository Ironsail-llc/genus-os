#!/usr/bin/env python3
"""
CRM Consistency Check for Robothor.

Runs daily at 3:15 AM (after maintenance.sh at 3:00, before intelligence_pipeline at 3:30).
Pure Python checks — no AI, no LLM calls.

Checks:
1. contact_identifiers entries have valid person_id references
2. crm_people records have matching contact_identifiers entries
3. crm_conversations have valid person_id references
4. Bridge health check
5. Log discrepancies
"""

import json
import logging
from datetime import datetime

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}


def check_contact_identifiers() -> dict:
    """Check contact_identifiers table for orphaned or stale entries."""
    result = {"total": 0, "issues": []}

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as cnt FROM contact_identifiers")
        result["total"] = cur.fetchone()["cnt"]

        # Check for entries missing person_id
        cur.execute("""
            SELECT channel, identifier FROM contact_identifiers
            WHERE person_id IS NULL
        """)
        orphans = cur.fetchall()
        if orphans:
            for o in orphans:
                result["issues"].append(
                    f"Orphan: {o['channel']}:{o['identifier']} has no person_id"
                )

        conn.close()
    except Exception as e:
        result["issues"].append(f"DB error: {e}")

    return result


def check_crm_people_integrity() -> dict:
    """Verify crm_people records are consistent with contact_identifiers."""
    result = {"people_count": 0, "unlinked": 0, "issues": []}

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as cnt FROM crm_people WHERE deleted_at IS NULL")
        result["people_count"] = cur.fetchone()["cnt"]

        # Check for people without any contact_identifiers entry
        cur.execute("""
            SELECT p.id, p.first_name, p.last_name
            FROM crm_people p
            LEFT JOIN contact_identifiers ci ON ci.person_id = p.id
            WHERE p.deleted_at IS NULL AND ci.id IS NULL
            LIMIT 10
        """)
        unlinked = cur.fetchall()
        result["unlinked"] = len(unlinked)
        for u in unlinked:
            result["issues"].append(
                f"Unlinked person: {u['first_name']} {u['last_name']} ({u['id']}) has no contact_identifiers"
            )

        conn.close()
    except Exception as e:
        result["issues"].append(f"DB error: {e}")

    return result


def check_conversations_integrity() -> dict:
    """Verify crm_conversations reference valid people."""
    result = {"conversation_count": 0, "orphaned": 0, "issues": []}

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as cnt FROM crm_conversations")
        result["conversation_count"] = cur.fetchone()["cnt"]

        # Check for conversations with invalid person_id
        cur.execute("""
            SELECT c.id, c.person_id
            FROM crm_conversations c
            LEFT JOIN crm_people p ON p.id = c.person_id
            WHERE c.person_id IS NOT NULL AND p.id IS NULL
            LIMIT 10
        """)
        orphaned = cur.fetchall()
        result["orphaned"] = len(orphaned)
        for o in orphaned:
            result["issues"].append(
                f"Orphaned conversation {o['id']} references missing person {o['person_id']}"
            )

        conn.close()
    except Exception as e:
        result["issues"].append(f"DB error: {e}")

    return result


def check_bridge_health() -> dict:
    """Check bridge service health endpoint."""
    result = {"healthy": False, "details": ""}

    try:
        _bridge_health = None
        try:
            from memory_system.service_registry import get_health_url

            _bridge_health = get_health_url("bridge")
        except ImportError:
            pass
        resp = requests.get(_bridge_health or "http://localhost:9100/health", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            result["healthy"] = data.get("status") == "ok"
            result["details"] = json.dumps(data)
        else:
            result["details"] = f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        result["details"] = f"Bridge unreachable: {e}"

    return result


def main():
    start_time = datetime.now()
    logger.info("=== CRM Consistency Check Started: %s ===", start_time)

    all_issues = []

    # 1. Contact identifiers table
    logger.info("Check 1: Contact identifiers table...")
    ci_result = check_contact_identifiers()
    logger.info("  → %d entries, %d issues", ci_result["total"], len(ci_result["issues"]))
    all_issues.extend(ci_result["issues"])

    # 2. CRM people integrity
    logger.info("Check 2: CRM people integrity...")
    people_result = check_crm_people_integrity()
    logger.info(
        "  → %d people, %d unlinked", people_result["people_count"], people_result["unlinked"]
    )
    all_issues.extend(people_result["issues"])

    # 3. Conversations integrity
    logger.info("Check 3: Conversations integrity...")
    conv_result = check_conversations_integrity()
    logger.info(
        "  → %d conversations, %d orphaned",
        conv_result["conversation_count"],
        conv_result["orphaned"],
    )
    all_issues.extend(conv_result["issues"])

    # 4. Bridge health
    logger.info("Check 4: Bridge health...")
    bridge_result = check_bridge_health()
    logger.info("  → healthy: %s", bridge_result["healthy"])
    if not bridge_result["healthy"]:
        all_issues.append(f"Bridge unhealthy: {bridge_result['details']}")

    # Summary
    duration = (datetime.now() - start_time).total_seconds()
    logger.info("=== Consistency Check Complete (%.1fs) — %d issues ===", duration, len(all_issues))

    if all_issues:
        logger.warning("ISSUES FOUND:")
        for issue in all_issues:
            logger.warning("  - %s", issue)
    else:
        logger.info("All checks passed — 0 discrepancies")

    print(f"CRM Consistency Check — {start_time.date()}")
    print(f"  Contact identifiers: {ci_result['total']}")
    print(f"  CRM people: {people_result['people_count']} ({people_result['unlinked']} unlinked)")
    print(
        f"  Conversations: {conv_result['conversation_count']} ({conv_result['orphaned']} orphaned)"
    )
    print(f"  Bridge: {'healthy' if bridge_result['healthy'] else 'UNHEALTHY'}")
    print(f"  Issues: {len(all_issues)}")
    print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    main()
