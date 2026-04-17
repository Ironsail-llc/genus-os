#!/usr/bin/env python3
"""
Tier 2: Periodic Analysis — runs 4x daily (7, 11, 15, 19).

Absorbs time-sensitive analysis from the nightly pipeline:
- Meeting prep briefs (for meetings in next 6 hours)
- Memory block maintenance (working_context, contacts_summary, operational_findings)
- Entity graph enrichment (link unlinked facts from last 4 hours)

Replaces midday_intelligence.py.

Cron: 0 7,11,15,19 * * *
Expected: 3-8 minutes per run.
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection

MEMORY_DIR = Path.home() / "robothor" / "brain" / "memory"
MEMORY_SYSTEM_DIR = Path.home() / "robothor" / "brain" / "memory_system"
LOGS_DIR = MEMORY_SYSTEM_DIR / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

LOGS_DIR.mkdir(exist_ok=True)


async def load_llm_client():
    from robothor.llm import ollama as llm_client

    return llm_client


# ═══════════════════════════════════════════════════════════════════
# Meeting Prep (from Phase 8)
# ═══════════════════════════════════════════════════════════════════


async def meeting_prep(llm_client) -> dict[str, Any]:
    """Generate prep briefs for meetings in the next 6 hours."""
    results = {"meetings_found": 0, "briefs_generated": 0, "errors": []}

    calendar_path = MEMORY_DIR / "calendar-log.json"
    if not calendar_path.exists():
        return results

    calendar_data = json.loads(calendar_path.read_text())
    now = datetime.now()
    cutoff = now + timedelta(hours=6)

    upcoming = []
    for event_id, event in calendar_data.get("entries", {}).items():
        start = event.get("start", "")
        if not start:
            continue
        try:
            event_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if event_time.tzinfo:
                event_time = event_time.replace(tzinfo=None)
            if now < event_time <= cutoff:
                upcoming.append(event)
        except (ValueError, TypeError):
            continue

    results["meetings_found"] = len(upcoming)
    if not upcoming:
        return results

    # Load contact profiles for attendee context
    try:
        from crm_fetcher import fetch_all_contacts

        crm_contacts = fetch_all_contacts()
        contacts_by_email = {c["email"]: c for c in crm_contacts if c.get("email")}
    except Exception:
        contacts_by_email = {}

    email_log_path = MEMORY_DIR / "email-log.json"
    email_entries = {}
    if email_log_path.exists():
        email_entries = json.loads(email_log_path.read_text()).get("entries", {})

    for meeting in upcoming:
        try:
            attendees = meeting.get("attendees", [])
            summary = meeting.get("summary", "Meeting")
            start_time = meeting.get("start", "")

            attendee_context = []
            for att in attendees[:5]:
                email = att if isinstance(att, str) else att.get("email", "")
                profile = contacts_by_email.get(email, {})

                att_info = f"  - {email}"
                if profile:
                    att_info += f" ({profile.get('firstName', '')} {profile.get('lastName', '')}"
                    if profile.get("jobTitle"):
                        att_info += f", {profile['jobTitle']}"
                    if profile.get("company"):
                        att_info += f" at {profile['company']}"
                    att_info += ")"

                recent = [e for e in email_entries.values() if email in e.get("from", "")][-2:]
                for e in recent:
                    att_info += (
                        f'\n    Email: "{e.get("subject", "")}" ({e.get("processedAt", "")[:10]})'
                    )

                attendee_context.append(att_info)

            context = f"""Meeting: {summary}
Time: {start_time}
Attendees:
{chr(10).join(attendee_context)}"""

            brief = await llm_client.generate(
                prompt=f"""Generate a meeting prep brief (2-3 paragraphs) covering:
- Who's attending and their context
- Recent interactions with attendees
- Likely topics based on recent communications
- Suggested talking points

{context}

Write the brief directly.""",
                system="You are an executive assistant preparing meeting briefs. Be concise and actionable.",
                temperature=0.3,
                max_tokens=500,
            )

            from robothor.memory.ingestion import ingest_content

            await ingest_content(
                content=f"Meeting Prep Brief — {summary} ({start_time[:10]}): {brief}",
                source_channel="crm",
                content_type="event",
                metadata={
                    "type": "meeting_prep",
                    "meeting_summary": summary,
                    "meeting_date": start_time[:10],
                    "generated_at": datetime.now().isoformat(),
                },
            )
            results["briefs_generated"] += 1

        except Exception as e:
            results["errors"].append(f"meeting:{meeting.get('summary', '?')}:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# Memory Block Maintenance (from Phase 10)
# ═══════════════════════════════════════════════════════════════════


def _write_memory_block(block_name: str, content: str):
    """Write content to a memory block in the database."""
    from robothor.memory.blocks import write_block

    write_block(block_name, content[:5000])


async def memory_blocks(llm_client) -> dict[str, Any]:
    """Regenerate key memory blocks with fresh data."""
    results = {"blocks_updated": 0, "errors": []}

    # --- contacts_summary ---
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT fact_text FROM memory_facts
            WHERE source_type = 'engagement_score'
              AND created_at > NOW() - INTERVAL '2 days'
            ORDER BY created_at DESC
            LIMIT 50
        """)
        scores = [r["fact_text"] for r in cur.fetchall()]

        cur.execute("""
            SELECT fact_text FROM memory_facts
            WHERE metadata->>'type' = 'relationship_brief'
              AND created_at > NOW() - INTERVAL '2 days'
            ORDER BY created_at DESC
            LIMIT 20
        """)
        briefs = [r["fact_text"] for r in cur.fetchall()]
        conn.close()

        if scores or briefs:
            input_text = "Engagement scores:\n" + "\n".join(scores[:20])
            if briefs:
                input_text += "\n\nRelationship briefs:\n" + "\n".join(briefs[:10])

            summary = await llm_client.generate(
                prompt=f"""Synthesize a contacts summary (max 4500 chars) covering:
- Top contacts by engagement
- Key relationships and their current status
- Recent changes in engagement levels
- Contacts needing follow-up

Data:
{input_text}

Write a concise, structured summary.""",
                system="You are maintaining a contacts summary block. Be concise and organized.",
                temperature=0.3,
                max_tokens=1500,
            )
            _write_memory_block("contacts_summary", summary)
            results["blocks_updated"] += 1

    except Exception as e:
        logger.error("contacts_summary block update failed: %s", e)
        results["errors"].append(f"contacts_summary:{e}")

    # --- working_context ---
    try:
        context_parts = []

        tasks_path = MEMORY_DIR / "tasks.json"
        if tasks_path.exists():
            tasks = json.loads(tasks_path.read_text()).get("tasks", [])
            active = [t for t in tasks if t.get("status") != "completed"]
            if active:
                context_parts.append(
                    "Active tasks: "
                    + ", ".join([t.get("description", "?")[:60] for t in active[:5]])
                )

        calendar_path = MEMORY_DIR / "calendar-log.json"
        if calendar_path.exists():
            cal = json.loads(calendar_path.read_text())
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            meetings = [
                e
                for e in cal.get("entries", {}).values()
                if e.get("start", "").startswith(today) or e.get("start", "").startswith(tomorrow)
            ]
            if meetings:
                context_parts.append(
                    "Upcoming meetings: " + ", ".join([m.get("summary", "?") for m in meetings[:5]])
                )

        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT fact_text FROM memory_facts
            WHERE metadata->>'type' = 'cross_system_pattern'
              AND created_at > NOW() - INTERVAL '2 days'
            ORDER BY created_at DESC
            LIMIT 10
        """)
        patterns = [r["fact_text"] for r in cur.fetchall()]
        conn.close()

        if patterns:
            context_parts.append("Patterns detected:\n" + "\n".join(patterns[:5]))

        if context_parts:
            input_text = "\n\n".join(context_parts)
            wc = await llm_client.generate(
                prompt=f"""Generate a working context summary (max 4500 chars) covering:
- Active projects and their status
- Upcoming meetings with prep notes
- Outstanding items and follow-ups
- Recent decisions and patterns

Data:
{input_text}

Write a concise handoff note for the next session.""",
                system="You are maintaining a working context block. Be concise and actionable.",
                temperature=0.3,
                max_tokens=1500,
            )
            _write_memory_block("working_context", wc)
            results["blocks_updated"] += 1

    except Exception as e:
        logger.error("working_context block update failed: %s", e)
        results["errors"].append(f"working_context:{e}")

    # --- operational_findings ---
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT fact_text FROM memory_facts
            WHERE (category IN ('technical', 'project') OR source_type = 'decision')
              AND created_at > NOW() - INTERVAL '7 days'
            ORDER BY created_at DESC
            LIMIT 20
        """)
        tech_facts = [r["fact_text"] for r in cur.fetchall()]
        conn.close()

        if tech_facts:
            input_text = "\n".join(tech_facts)
            of = await llm_client.generate(
                prompt=f"""Update the operational findings block (max 4500 chars) with system learnings from recent technical and decision facts:

{input_text}

Summarize key findings, lessons learned, and system quirks worth remembering.""",
                system="You are maintaining operational findings. Focus on reusable insights.",
                temperature=0.3,
                max_tokens=1500,
            )
            _write_memory_block("operational_findings", of)
            results["blocks_updated"] += 1

    except Exception as e:
        logger.error("operational_findings block update failed: %s", e)
        results["errors"].append(f"operational_findings:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# Entity Graph Enrichment (from Phase 11)
# ═══════════════════════════════════════════════════════════════════


async def entity_enrichment() -> dict[str, Any]:
    """Process unlinked facts from the last 4 hours."""
    results = {"facts_processed": 0, "entities_added": 0, "relations_added": 0, "errors": []}

    try:
        from robothor.memory.entities import extract_entities_batch

        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT f.id FROM memory_facts f
            WHERE f.created_at > NOW() - INTERVAL '4 hours'
              AND (f.entities IS NULL OR array_length(f.entities, 1) IS NULL)
            LIMIT 50
        """)
        unlinked_ids = [r["id"] for r in cur.fetchall()]
        conn.close()

        results["facts_processed"] = len(unlinked_ids)

        if unlinked_ids:
            entity_results = await extract_entities_batch(unlinked_ids)
            results["entities_added"] = entity_results.get("entities_stored", 0)
            results["relations_added"] = entity_results.get("relations_stored", 0)

    except Exception as e:
        logger.error("Entity enrichment failed: %s", e)
        results["errors"].append(f"entity_enrichment:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# Contact Reconciliation (Phase 4)
# ═══════════════════════════════════════════════════════════════════


def contact_reconciliation() -> dict[str, Any]:
    """Reconcile memory entities with CRM contacts.

    1. Link memory_entity_id in contact_identifiers (fuzzy name matching)
    2. Discover new contacts from high-mention entities and recent meeting attendees
    3. Create missing CRM records for discovered contacts
    """
    results = {
        "entities_linked": 0,
        "contacts_discovered": 0,
        "contacts_created": 0,
        "errors": [],
    }

    try:
        from robothor.crm.dal import get_owner_person
        from robothor.memory.contact_matching import find_best_match
        from robothor.owner_config import load_owner_config

        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Resolve the operator's memory_entities row + nickname set once.
        # Without this, find_best_match can't prefer the owner on name-only
        # ties (e.g. "Alice" matching both "Alice Owner" and "Alice Example"
        # with equal score), so the tiebreak added in PR #97 was unreachable
        # from production. See robothor/memory/contact_matching.find_best_match.
        owner_candidate_id = None
        owner_nicknames: set[str] = set()
        try:
            owner_cfg = load_owner_config()
            owner_person = get_owner_person(tenant_id=owner_cfg.tenant_id)
            if owner_person:
                # Look up the memory_entities row that mirrors the owner,
                # keyed by name (periodic_analysis runs against memory_entities).
                cur.execute(
                    """
                    SELECT id FROM memory_entities
                    WHERE entity_type = 'person'
                      AND LOWER(name) = LOWER(%s)
                    LIMIT 1
                    """,
                    (f"{owner_person['first_name']} {owner_person['last_name']}".strip(),),
                )
                owner_row = cur.fetchone()
                if owner_row:
                    owner_candidate_id = owner_row["id"]
            owner_nicknames = {
                n.lower()
                for n in (
                    *owner_cfg.nicknames,
                    owner_cfg.first_name,
                    owner_cfg.last_name,
                )
                if n
            }
        except Exception as e:
            logger.debug("owner context unavailable for find_best_match tiebreak: %s", e)

        # --- Reconciliation: link memory_entity_id ---

        # Get contact_identifiers rows with NULL memory_entity_id
        # Skip service accounts and bots by filtering known non-person names
        cur.execute("""
            SELECT id, display_name, channel, identifier, person_id
            FROM contact_identifiers
            WHERE memory_entity_id IS NULL
              AND display_name NOT IN ('test', 'Gemini', 'LinkedIn', 'robothor', 'OpenRouter Team')
        """)
        unlinked = cur.fetchall()

        if unlinked:
            # Get all person entities from memory
            cur.execute("""
                SELECT id, name, entity_type, mention_count, aliases
                FROM memory_entities
                WHERE entity_type = 'person'
            """)
            person_entities = cur.fetchall()

            for row in unlinked:
                display_name = row.get("display_name", "")
                if not display_name:
                    continue

                match = find_best_match(
                    display_name,
                    person_entities,
                    threshold=0.75,
                    owner_candidate_id=owner_candidate_id,
                    owner_nicknames=owner_nicknames,
                )

                if match:
                    cur.execute(
                        """
                        UPDATE contact_identifiers
                        SET memory_entity_id = %s, updated_at = NOW()
                        WHERE id = %s AND memory_entity_id IS NULL
                    """,
                        (match["id"], row["id"]),
                    )
                    results["entities_linked"] += 1
                    logger.info(
                        "  Linked '%s' → entity '%s' (score=%.2f)",
                        display_name,
                        match["name"],
                        match["match_score"],
                    )

            conn.commit()

        # --- Discovery: find people who should be in CRM ---

        # Get entity IDs already in contact_identifiers
        cur.execute("""
            SELECT DISTINCT memory_entity_id FROM contact_identifiers
            WHERE memory_entity_id IS NOT NULL
        """)
        linked_entity_ids = {r["memory_entity_id"] for r in cur.fetchall()}

        # Get display_names already in contact_identifiers (for name-based dedup)
        cur.execute("SELECT DISTINCT display_name FROM contact_identifiers")
        existing_names = {r["display_name"].lower() for r in cur.fetchall() if r["display_name"]}

        # Also get existing CRM contact names to avoid creating duplicates
        try:
            from crm_fetcher import fetch_all_contacts

            crm_contacts = fetch_all_contacts()
            for c in crm_contacts:
                full = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip().lower()
                if full:
                    existing_names.add(full)
                # Also add individual first names for matching
                first = c.get("firstName", "").strip().lower()
                if first:
                    existing_names.add(first)
        except Exception:
            pass

        # High-mention person entities not already linked
        cur.execute("""
            SELECT id, name, entity_type, mention_count, aliases
            FROM memory_entities
            WHERE entity_type = 'person'
              AND mention_count >= 5
            ORDER BY mention_count DESC
        """)
        high_mention_entities = cur.fetchall()

        # Also gather recent meeting attendees
        meeting_attendees = set()
        transcripts_path = MEMORY_DIR / "meet-transcripts.json"
        if transcripts_path.exists():
            try:
                transcripts_data = json.loads(transcripts_path.read_text())
                # Format: {entries: {doc_id: {date, attendees, ...}}}
                entries = transcripts_data.get("entries", {})
                if isinstance(entries, dict):
                    entries = entries.values()
                cutoff = datetime.now() - timedelta(days=7)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    date_str = entry.get("date", "")
                    if date_str:
                        try:
                            meeting_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                            if meeting_date.tzinfo:
                                meeting_date = meeting_date.replace(tzinfo=None)
                            if meeting_date < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass
                    for attendee in entry.get("attendees", []):
                        if attendee and len(attendee) > 2:
                            meeting_attendees.add(attendee)
            except (json.JSONDecodeError, TypeError):
                pass

        # Build candidates list from existing names for fuzzy dedup
        existing_name_list = [{"name": n} for n in existing_names]

        # Build set of known last names to detect reversed/artifact names
        known_last_names = set()
        try:
            for c in crm_contacts:
                ln = c.get("lastName", "").strip().lower()
                if ln:
                    known_last_names.add(ln)
        except Exception:
            pass

        # Process high-mention entities
        discovered = []
        for entity in high_mention_entities:
            if entity["id"] in linked_entity_ids:
                continue
            if entity["name"].lower() in existing_names:
                continue
            # Fuzzy check against existing CRM contacts
            if find_best_match(entity["name"], existing_name_list, threshold=0.8):
                continue
            # Skip reversed names (last name first) — parsing artifacts
            name_parts = entity["name"].split()
            if len(name_parts) >= 2 and name_parts[0].lower() in known_last_names:
                logger.debug("  Skipping reversed entity name: '%s'", entity["name"])
                continue
            # Skip generic single-word names that are too common
            if len(name_parts) == 1 and entity["mention_count"] < 10:
                continue

            discovered.append(entity)
            results["contacts_discovered"] += 1

        # Process meeting attendees not in CRM
        for attendee in meeting_attendees:
            if attendee.lower() in existing_names:
                continue
            # Fuzzy check against existing CRM contacts
            if find_best_match(attendee, existing_name_list, threshold=0.8):
                continue
            # Skip reversed names like "Doe Smith" or "Johnson Jane"
            att_parts = attendee.split()
            if len(att_parts) >= 2 and att_parts[0].lower() in known_last_names:
                logger.debug("  Skipping reversed name: '%s'", attendee)
                continue
            # Check if already in discovered entities
            if discovered and find_best_match(
                attendee, [{"name": d["name"]} for d in discovered], threshold=0.85
            ):
                continue

            discovered.append(
                {
                    "id": None,
                    "name": attendee,
                    "mention_count": 1,
                    "source": "meeting",
                }
            )
            results["contacts_discovered"] += 1

        # Create CRM records for discovered contacts
        if discovered:
            _create_crm_records(discovered, cur, conn, results)

        conn.close()

    except Exception as e:
        logger.error("Contact reconciliation failed: %s", e)
        results["errors"].append(str(e))

    return results


def _create_crm_records(
    discovered: list,
    cur,
    conn,
    results: dict[str, Any],
):
    """Create CRM person records and contact_identifiers rows via crm_dal."""
    import os

    sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
    import crm_dal

    for entity in discovered[:20]:  # Cap at 20 per run
        name = entity["name"]
        name_parts = name.split(None, 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        try:
            person_id = crm_dal.create_person(first_name, last_name)

            if person_id:
                # Create contact_identifiers row
                cur.execute(
                    """
                    INSERT INTO contact_identifiers
                        (channel, identifier, display_name, person_id, memory_entity_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (channel, identifier) DO NOTHING
                """,
                    ("crm", f"person:{person_id}", name, person_id, entity.get("id")),
                )
                conn.commit()
                results["contacts_created"] += 1
                logger.info("  Created CRM record for '%s' (person_id=%s)", name, person_id)
            else:
                logger.warning("  Failed to create CRM record for '%s'", name)

        except Exception as e:
            results["errors"].append(f"create:{name}:{e}")
            logger.error("  Error creating CRM record for '%s': %s", name, e)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


async def main():
    start_time = datetime.now()
    logger.info("═══ Periodic Analysis Started: %s ═══", start_time)

    llm_client = await load_llm_client()

    report = {}

    logger.info("Phase 1: Meeting Prep Briefs...")
    report["meetings"] = await meeting_prep(llm_client)
    logger.info(
        "  → %d meetings, %d briefs",
        report["meetings"]["meetings_found"],
        report["meetings"]["briefs_generated"],
    )

    logger.info("Phase 2: Memory Block Maintenance...")
    report["blocks"] = await memory_blocks(llm_client)
    logger.info("  → %d blocks updated", report["blocks"]["blocks_updated"])

    logger.info("Phase 3: Entity Graph Enrichment...")
    report["entities"] = await entity_enrichment()
    logger.info(
        "  → %d facts → %d entities, %d relations",
        report["entities"]["facts_processed"],
        report["entities"]["entities_added"],
        report["entities"]["relations_added"],
    )

    logger.info("Phase 4: Contact Reconciliation...")
    report["contacts"] = contact_reconciliation()
    logger.info(
        "  → %d linked, %d discovered, %d created",
        report["contacts"]["entities_linked"],
        report["contacts"]["contacts_discovered"],
        report["contacts"]["contacts_created"],
    )

    duration = (datetime.now() - start_time).total_seconds()
    logger.info("═══ Periodic Analysis Complete (%.1fs) ═══", duration)

    print(f"Periodic Analysis — {start_time.strftime('%H:%M')}")
    print(f"  Meeting preps: {report['meetings']['briefs_generated']}")
    print(f"  Blocks updated: {report['blocks']['blocks_updated']}")
    print(f"  Entities added: {report['entities']['entities_added']}")
    print(f"  Contacts linked: {report['contacts']['entities_linked']}")
    print(f"  Contacts created: {report['contacts']['contacts_created']}")
    print(f"  Duration: {duration:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
