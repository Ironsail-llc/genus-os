#!/usr/bin/env python3
"""
Tier 3: Deep Analysis Pipeline — runs daily at 3:30 AM.

Part of the three-tier intelligence architecture:
  Tier 1: continuous_ingest.py (*/10 min) — incremental deduped ingestion
  Tier 2: periodic_analysis.py (4x daily) — meeting prep, memory blocks, entities
  Tier 3: THIS FILE (daily 3:30 AM) — deep analysis, patterns, quality

Phases (deep analysis only — ingestion moved to Tier 1):
    Phase 1: Catch-up retry — re-attempt failed ingestions from watermarks
    Phase 2: Relationship Intelligence — per-contact analysis (~10 min)
    Phase 3: Contact Engagement Scoring (~3 min)
    Phase 4: Cross-System Pattern Detection (~5 min)
    Phase 5: Lifecycle maintenance + quality tests (~5 min)
    Phase 6: Cleanup — prune old ingested_items (>90 days)

Usage:
    cd /home/philip/robothor/brain/memory_system
    source venv/bin/activate
    python intelligence_pipeline.py

Model: Llama 3.2 Vision 11B (44 tok/s on GB10, clean structured output)
100% local, zero API cost, no cloud dependencies.
"""

import asyncio
import fcntl
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Paths
MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
MEMORY_SYSTEM_DIR = Path("/home/philip/robothor/brain/memory_system")
LOGS_DIR = MEMORY_SYSTEM_DIR / "logs"
QUALITY_LOG = MEMORY_DIR / "rag-quality-log.json"
NIGHTLY_LOCK = MEMORY_SYSTEM_DIR / "locks" / "nightly_pipeline.lock"

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
}

# Ensure dirs exist
LOGS_DIR.mkdir(exist_ok=True)
NIGHTLY_LOCK.parent.mkdir(exist_ok=True)


async def load_llm_client():
    """Import llm_client."""
    from robothor.llm import ollama as llm_client

    return llm_client


# ═══════════════════════════════════════════════════════════════════
# PHASE 1: Catch-up Retry — re-attempt failed ingestions
# ═══════════════════════════════════════════════════════════════════


async def phase_1_catchup_retry() -> dict[str, Any]:
    """Check ingestion_watermarks for sources with errors, trigger re-ingestion."""
    results = {"sources_retried": 0, "errors": []}

    try:
        from robothor.memory.ingest_state import get_watermark, update_watermark

        sources = ["email", "calendar", "tasks", "jira", "conversation", "twenty_crm", "contacts"]

        for source in sources:
            wm = get_watermark(source)
            if wm and wm["error_count"] > 0:
                logger.info("  Source %s has %d errors, retrying...", source, wm["error_count"])
                results["sources_retried"] += 1
                # Reset error count — Tier 1 will retry on next run
                update_watermark(source, 0)

    except Exception as e:
        logger.error("Catch-up retry failed: %s", e)
        results["errors"].append(str(e))

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 2: Relationship Intelligence (Llama, ~10 min)
# ═══════════════════════════════════════════════════════════════════


async def phase_2_relationship_intelligence(llm_client) -> dict[str, Any]:
    """Per-contact relationship analysis using Llama."""
    results = {"contacts_analyzed": 0, "briefs_generated": 0, "errors": []}

    try:
        from crm_fetcher import fetch_all_contacts, fetch_conversations

        from robothor.memory.facts import search_facts

        contacts = fetch_all_contacts()
        conversations = fetch_conversations(hours=168)  # 7 days

        # Index conversations by contact email
        conv_by_email = {}
        for conv in conversations:
            email = conv.get("contact_email", "")
            if email:
                conv_by_email.setdefault(email, []).append(conv)

        # Read email log for cross-referencing
        email_log_path = MEMORY_DIR / "email-log.json"
        email_entries = {}
        if email_log_path.exists():
            email_log = json.loads(email_log_path.read_text())
            email_entries = email_log.get("entries", {})

        for contact in contacts[:30]:  # cap at 30 contacts per run
            try:
                name = f"{contact['firstName']} {contact['lastName']}".strip()
                if not name:
                    continue

                results["contacts_analyzed"] += 1

                # Gather data from all sources
                contact_convs = conv_by_email.get(contact.get("email", ""), [])
                contact_emails = [
                    e
                    for e in email_entries.values()
                    if contact.get("email", "N/A") in e.get("from", "")
                ][-5:]  # last 5 emails

                memory_facts = await search_facts(name, limit=5)

                # Build context for Llama
                context_parts = [f"Contact: {name}"]
                if contact.get("jobTitle"):
                    context_parts.append(f"Title: {contact['jobTitle']}")
                if contact.get("company"):
                    context_parts.append(f"Company: {contact['company']}")

                if contact_emails:
                    context_parts.append(f"\nRecent emails ({len(contact_emails)}):")
                    for e in contact_emails[-3:]:
                        context_parts.append(
                            f"  - {e.get('subject', '')} ({e.get('processedAt', '')[:10]})"
                        )

                if contact_convs:
                    context_parts.append(f"\nCRM conversations ({len(contact_convs)}):")
                    for c in contact_convs[-3:]:
                        last_msg = c["messages"][-1]["content"][:100] if c.get("messages") else ""
                        context_parts.append(f"  - Conv #{c['id']}: {last_msg}")

                if memory_facts:
                    context_parts.append(f"\nMemory facts ({len(memory_facts)}):")
                    for f in memory_facts[:3]:
                        context_parts.append(f"  - {f.get('fact_text', '')[:100]}")

                context = "\n".join(context_parts)

                # Skip contacts with no recent activity
                if not contact_emails and not contact_convs and not memory_facts:
                    continue

                brief = await llm_client.generate(
                    prompt=f"""Analyze this contact's relationship with Philip. Write a concise brief (3-5 sentences) covering:
- Engagement level (how often they interact)
- Key topics or threads discussed recently
- Any open items or follow-ups needed
- Suggested next action

Contact data:
{context}

Write the brief directly, no preamble.""",
                    system="You are an executive assistant analyzing contact relationships. Be concise and actionable.",
                    temperature=0.3,
                    max_tokens=300,
                )

                # Store as fact
                try:
                    from robothor.memory.ingestion import ingest_content

                    await ingest_content(
                        content=f"Relationship Brief for {name}: {brief}",
                        source_channel="crm",
                        content_type="contact",
                        metadata={
                            "type": "relationship_brief",
                            "contact_name": name,
                            "contact_email": contact.get("email"),
                            "generated_at": datetime.now().isoformat(),
                        },
                    )
                    results["briefs_generated"] += 1
                except Exception as e:
                    results["errors"].append(f"brief_store:{name}:{e}")

            except Exception as e:
                results["errors"].append(f"contact_analysis:{contact.get('email', 'unknown')}:{e}")

    except Exception as e:
        logger.error("Phase 2 (Relationship Intelligence) failed: %s", e)
        results["errors"].append(f"phase2:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 2.5: Contact Enrichment (deterministic + Llama)
# ═══════════════════════════════════════════════════════════════════

# Common email domain → company mappings for deterministic extraction
DOMAIN_COMPANY_MAP = {
    "ironsail.ai": "Ironsail",
    "ironsailpharma.com": "Ironsail Pharma",
    "getdrx.com": "GetDRx",
    "google.com": "Google",
    "microsoft.com": "Microsoft",
}


def _extract_company_from_email(email: str) -> str | None:
    """Extract company name from email domain (deterministic)."""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower()
    # Check known mappings first
    if domain in DOMAIN_COMPANY_MAP:
        return DOMAIN_COMPANY_MAP[domain]
    # Skip common free email providers
    free_providers = {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "icloud.com",
        "aol.com",
        "protonmail.com",
    }
    if domain in free_providers:
        return None
    # Use domain name as company (capitalize, strip TLD)
    parts = domain.split(".")
    if len(parts) >= 2:
        company = parts[-2].capitalize()
        if len(company) >= 3:
            return company
    return None


async def phase_2_5_contact_enrichment(llm_client) -> dict[str, Any]:
    """Enrich CRM contacts with information from memory facts, emails, and transcripts.

    Only fills empty fields — never overwrites existing data.
    Uses deterministic extraction first (email domain → company),
    then LLM for non-obvious fields (job title, city).
    """
    results = {"contacts_checked": 0, "fields_updated": 0, "errors": []}

    try:
        import sys

        from crm_fetcher import fetch_all_contacts

        sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
        import crm_dal

        contacts = fetch_all_contacts()

        # Load email log for cross-referencing
        email_entries = {}
        email_log_path = MEMORY_DIR / "email-log.json"
        if email_log_path.exists():
            email_entries = json.loads(email_log_path.read_text()).get("entries", {})

        # Load meeting transcripts for context
        transcript_excerpts = {}
        transcripts_path = MEMORY_DIR / "meet-transcripts.json"
        if transcripts_path.exists():
            try:
                transcripts_data = json.loads(transcripts_path.read_text())
                # Format: {entries: {doc_id: {date, attendees, ...}}}
                entries = transcripts_data.get("entries", {})
                if isinstance(entries, dict):
                    entries = entries.values()
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for attendee in entry.get("attendees", []):
                        if attendee:
                            key = attendee.lower()
                            if key not in transcript_excerpts:
                                transcript_excerpts[key] = []
                            # Store summary + decisions as context
                            excerpt = entry.get("summary", "")
                            decisions = entry.get("decisions", [])
                            if decisions:
                                excerpt += " Decisions: " + "; ".join(decisions[:3])
                            if excerpt:
                                transcript_excerpts[key].append(excerpt[:300])
            except (json.JSONDecodeError, TypeError):
                pass

        for contact in contacts:
            name = f"{contact['firstName']} {contact['lastName']}".strip()
            if not name:
                continue

            results["contacts_checked"] += 1

            has_job_title = bool(contact.get("jobTitle"))
            has_company = bool(contact.get("company"))
            has_city = bool(contact.get("city"))

            # Skip fully populated contacts
            if has_job_title and has_company and has_city:
                continue

            updates = {}

            # --- Deterministic: email domain → company ---
            if not has_company and contact.get("email"):
                company = _extract_company_from_email(contact["email"])
                if company:
                    try:
                        company_id = crm_dal.find_or_create_company(company)
                        if company_id:
                            updates["company_id"] = company_id
                    except Exception as e:
                        results["errors"].append(f"company_resolve:{name}:{e}")

            # --- LLM extraction for job title and city ---
            if not has_job_title or not has_city:
                # Gather evidence
                evidence_parts = []

                # Memory facts
                try:
                    conn = psycopg2.connect(**DB_CONFIG)
                    cur = conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute(
                        """
                        SELECT fact_text FROM memory_facts
                        WHERE %s = ANY(entities) OR fact_text ILIKE %s
                        ORDER BY created_at DESC LIMIT 10
                    """,
                        (name, f"%{name}%"),
                    )
                    facts = [r["fact_text"] for r in cur.fetchall()]
                    conn.close()
                    if facts:
                        evidence_parts.append(
                            "Memory facts:\n" + "\n".join(f"- {f[:200]}" for f in facts[:5])
                        )
                except Exception:
                    pass

                # Email subjects
                if contact.get("email"):
                    contact_emails = [
                        e for e in email_entries.values() if contact["email"] in e.get("from", "")
                    ][-5:]
                    if contact_emails:
                        evidence_parts.append(
                            "Emails:\n"
                            + "\n".join(
                                f"- {e.get('subject', '')} (from: {e.get('from', '')})"
                                for e in contact_emails
                            )
                        )

                # Meeting transcript excerpts
                name_lower = name.lower()
                if name_lower in transcript_excerpts:
                    excerpts = transcript_excerpts[name_lower][:3]
                    evidence_parts.append(
                        "Meeting context:\n" + "\n".join(f"- {e}" for e in excerpts)
                    )

                if not evidence_parts:
                    # No evidence to extract from — skip LLM call
                    pass
                else:
                    evidence = "\n\n".join(evidence_parts)

                    try:
                        extraction = await llm_client.generate(
                            prompt=f"""Extract structured information about {name} from the evidence below.

Evidence:
{evidence}

Reply with ONLY valid JSON (no markdown, no explanation):
{{"job_title": "their role/title or null", "company": "their company or null", "city": "their city or null", "confidence": 0.0}}

Set confidence to a value between 0.0 and 1.0 based on how sure you are.
Only include fields you're confident about. Use null for uncertain fields.""",
                            system="You extract structured contact information from evidence. Be precise. Only state facts clearly supported by evidence.",
                            temperature=0.1,
                            max_tokens=150,
                        )

                        # Parse LLM response
                        try:
                            # Try to extract JSON from response
                            json_str = extraction.strip()
                            if "```" in json_str:
                                json_str = json_str.split("```")[1]
                                if json_str.startswith("json"):
                                    json_str = json_str[4:]
                                json_str = json_str.strip()

                            parsed = json.loads(json_str)
                            confidence = float(parsed.get("confidence", 0))

                            if confidence >= 0.7:
                                jt = parsed.get("job_title")
                                if not has_job_title and jt and jt != "null" and len(jt) > 1:
                                    updates["jobTitle"] = jt
                                ct = parsed.get("city")
                                if not has_city and ct and ct != "null" and len(ct) > 1:
                                    updates["city"] = ct
                                # company from LLM (only if not already set by deterministic)
                                co = parsed.get("company")
                                if (
                                    not has_company
                                    and "company_id" not in updates
                                    and co
                                    and co != "null"
                                    and len(co) > 1
                                ):
                                    try:
                                        company_id = crm_dal.find_or_create_company(co)
                                        if company_id:
                                            updates["company_id"] = company_id
                                    except Exception as e:
                                        results["errors"].append(f"llm_company:{name}:{e}")

                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass

                    except Exception as e:
                        results["errors"].append(f"llm_extract:{name}:{e}")

            # --- Apply updates to CRM ---
            if updates:
                try:
                    ok = crm_dal.update_person(
                        contact["id"],
                        job_title=updates.get("jobTitle"),
                        company_id=updates.get("company_id"),
                        city=updates.get("city"),
                    )
                    if ok:
                        results["fields_updated"] += len(updates)
                        logger.info("  Enriched '%s': %s", name, list(updates.keys()))
                    else:
                        logger.warning("  Failed to update '%s'", name)
                except Exception as e:
                    results["errors"].append(f"update:{name}:{e}")

    except Exception as e:
        logger.error("Phase 2.5 (Contact Enrichment) failed: %s", e)
        results["errors"].append(f"phase2.5:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 3: Contact Engagement Scoring (Llama, ~3 min)
# ═══════════════════════════════════════════════════════════════════


async def phase_3_engagement_scoring(llm_client) -> dict[str, Any]:
    """Score contact engagement levels using Llama."""
    results = {"scored": 0, "changed": 0, "errors": []}

    try:
        from crm_fetcher import fetch_all_contacts, fetch_conversations

        contacts = fetch_all_contacts()
        conversations = fetch_conversations(hours=720)  # 30 days

        # Count interactions per contact email
        interaction_counts = {}
        for conv in conversations:
            email = conv.get("contact_email", "")
            if email:
                msg_count = len([m for m in conv.get("messages", []) if m.get("type") == 0])
                interaction_counts[email] = interaction_counts.get(email, 0) + msg_count

        # Read email log for counts
        email_log_path = MEMORY_DIR / "email-log.json"
        if email_log_path.exists():
            email_log = json.loads(email_log_path.read_text())
            cutoff_30d = datetime.now() - timedelta(days=30)
            for entry in email_log.get("entries", {}).values():
                from_addr = entry.get("from", "")
                processed_at = entry.get("processedAt", "")
                if processed_at:
                    try:
                        t = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
                        if t.tzinfo:
                            t = t.replace(tzinfo=None)
                        if t >= cutoff_30d:
                            interaction_counts[from_addr] = interaction_counts.get(from_addr, 0) + 1
                    except (ValueError, TypeError):
                        pass

        # Batch contacts for scoring
        batch = []
        for contact in contacts[:50]:
            email = contact.get("email", "")
            count = interaction_counts.get(email, 0)
            name = f"{contact['firstName']} {contact['lastName']}".strip()
            if not name:
                continue

            days_since = 999
            if contact.get("updatedAt"):
                try:
                    updated = datetime.fromisoformat(contact["updatedAt"].replace("Z", "+00:00"))
                    if updated.tzinfo:
                        updated = updated.replace(tzinfo=None)
                    days_since = (datetime.now() - updated).days
                except (ValueError, TypeError):
                    pass

            batch.append(
                {
                    "name": name,
                    "email": email,
                    "interactions_30d": count,
                    "days_since_update": days_since,
                }
            )

        if not batch:
            return results

        batch_text = "\n".join(
            [
                f"- {c['name']} ({c['email']}): {c['interactions_30d']} interactions in 30d, last update {c['days_since_update']}d ago"
                for c in batch
            ]
        )

        scoring_response = await llm_client.generate(
            prompt=f"""Score each contact's engagement level based on their interaction data.
Levels: dormant (>30d no activity), low (7-30d), active (1-7d), high (<1d or >5 interactions/week)

Contacts:
{batch_text}

For each contact, output one line: NAME | LEVEL | brief reason
Example: John Smith | active | 3 emails this week about project X""",
            system="You are scoring contact engagement. Be precise and concise.",
            temperature=0.3,
            max_tokens=2000,
        )

        for line in scoring_response.strip().split("\n"):
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                name = parts[0]
                level = parts[1].lower().strip()
                reason = parts[2] if len(parts) > 2 else ""

                if level in ("dormant", "low", "active", "high"):
                    try:
                        conn = psycopg2.connect(**DB_CONFIG)
                        cur = conn.cursor()
                        cur.execute(
                            """
                            INSERT INTO memory_facts (fact_text, category, source_type, source_channel, confidence, metadata, is_active)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                        """,
                            (
                                f"Engagement score for {name}: {level} — {reason}",
                                "contact",
                                "engagement_score",
                                "crm",
                                0.8,
                                json.dumps(
                                    {
                                        "type": "engagement_score",
                                        "contact_name": name,
                                        "level": level,
                                        "scored_at": datetime.now().isoformat(),
                                    }
                                ),
                            ),
                        )
                        conn.commit()
                        conn.close()
                        results["scored"] += 1
                    except Exception as e:
                        results["errors"].append(f"score_store:{name}:{e}")

    except Exception as e:
        logger.error("Phase 3 (Engagement Scoring) failed: %s", e)
        results["errors"].append(f"phase3:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 4: Cross-System Pattern Detection (Llama, ~5 min)
# ═══════════════════════════════════════════════════════════════════


async def phase_4_pattern_detection(llm_client) -> dict[str, Any]:
    """Detect patterns across email, conversations, tasks, and CRM."""
    results = {"patterns_found": 0, "errors": []}

    try:
        sections = []

        # Emails
        email_log_path = MEMORY_DIR / "email-log.json"
        if email_log_path.exists():
            email_log = json.loads(email_log_path.read_text())
            cutoff_7d = datetime.now() - timedelta(days=7)
            recent_emails = []
            for entry in email_log.get("entries", {}).values():
                processed_at = entry.get("processedAt", "")
                if processed_at:
                    try:
                        t = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
                        if t.tzinfo:
                            t = t.replace(tzinfo=None)
                        if t >= cutoff_7d:
                            recent_emails.append(
                                f'  - {entry.get("from", "?")}: "{entry.get("subject", "")}" '
                                f"[{entry.get('urgency', 'low')}] ({processed_at[:10]})"
                            )
                    except (ValueError, TypeError):
                        pass
            if recent_emails:
                sections.append(
                    f"EMAILS ({len(recent_emails)} this week):\n" + "\n".join(recent_emails[:20])
                )

        # CRM conversations
        try:
            from crm_fetcher import fetch_conversations

            convs = fetch_conversations(hours=168)
            if convs:
                conv_summaries = []
                for c in convs[:10]:
                    last_msg = ""
                    if c.get("messages"):
                        last_msg = c["messages"][-1].get("content", "")[:80]
                    conv_summaries.append(f'  - {c["contact_name"]}: "{last_msg}" [{c["status"]}]')
                sections.append(f"CRM CONVERSATIONS ({len(convs)}):\n" + "\n".join(conv_summaries))
        except Exception:
            pass

        # Tasks
        tasks_path = MEMORY_DIR / "tasks.json"
        if tasks_path.exists():
            tasks_data = json.loads(tasks_path.read_text())
            active_tasks = [
                t for t in tasks_data.get("tasks", []) if t.get("status") != "completed"
            ]
            if active_tasks:
                task_lines = [
                    f"  - [{t.get('priority', 'med')}] {t.get('description', '')[:80]}"
                    for t in active_tasks[:10]
                ]
                sections.append(f"ACTIVE TASKS ({len(active_tasks)}):\n" + "\n".join(task_lines))

        if not sections:
            return results

        consolidated = "\n\n".join(sections)

        patterns = await llm_client.generate(
            prompt=f"""Analyze this week's activity across all systems. Identify:
1. Recurring themes or topics
2. Unresolved threads or dropped follow-ups
3. Contacts who need attention
4. Potential calendar conflicts or overdue items
5. Patterns worth noting

Data:
{consolidated}

List each pattern on its own line with a priority tag [HIGH/MED/LOW]. Be specific.""",
            system="You are analyzing cross-system activity patterns. Focus on actionable insights.",
            temperature=0.3,
            max_tokens=1000,
        )

        for line in patterns.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            priority = "medium"
            if "[HIGH]" in line.upper():
                priority = "high"
            elif "[LOW]" in line.upper():
                priority = "low"

            try:
                conn = psycopg2.connect(**DB_CONFIG)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO memory_facts (fact_text, category, source_type, source_channel, confidence, metadata, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """,
                    (
                        line,
                        "project",
                        "pattern_detection",
                        "crm",
                        0.7,
                        json.dumps(
                            {
                                "type": "cross_system_pattern",
                                "priority": priority,
                                "detected_at": datetime.now().isoformat(),
                            }
                        ),
                    ),
                )
                conn.commit()
                conn.close()
                results["patterns_found"] += 1
            except Exception as e:
                results["errors"].append(f"pattern_store:{e}")

    except Exception as e:
        logger.error("Phase 4 (Pattern Detection) failed: %s", e)
        results["errors"].append(f"phase4:{e}")

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 5: Lifecycle Maintenance + Quality Tests
# ═══════════════════════════════════════════════════════════════════


async def phase_5_housekeeping(llm_client) -> dict[str, Any]:
    """Run lifecycle maintenance and quality tests."""
    results = {"maintenance": {}, "quality": {}}

    # Lifecycle maintenance
    try:
        from robothor.memory.lifecycle import run_lifecycle_maintenance

        maintenance_results = await run_lifecycle_maintenance()
        results["maintenance"] = {"success": True, "details": str(maintenance_results)}
    except ImportError as e:
        logger.warning("lifecycle module not available: %s", e)
        results["maintenance"] = {"success": False, "details": f"import error: {e}"}
    except Exception as e:
        logger.error("Lifecycle maintenance error: %s", e)
        results["maintenance"] = {"success": False, "details": str(e)}

    # Quality tests
    try:
        from robothor.memory.facts import search_facts_compat as search_all_memory
    except ImportError:
        logger.warning("Could not import rag module for quality tests")
        return results

    test_queries = [
        "What decisions were made recently?",
        "Who are the key contacts and their engagement levels?",
        "What active tasks and Jira tickets need attention?",
        "What patterns were detected across email and CRM?",
        "What meeting prep or relationship briefs exist?",
    ]

    scores = []
    quality_tests = []
    for query in test_queries:
        try:
            search_results = search_all_memory(query, limit=5)
            if not search_results:
                quality_tests.append({"query": query, "score": 0, "note": "No results"})
                continue

            results_text = "\n".join(
                [f"- {r.get('content', '')[:200]}" for r in search_results[:3]]
            )

            eval_response = await llm_client.generate(
                prompt=f"""Rate the relevance of these search results on a scale of 1-5.
Query: {query}
Results:
{results_text}
Reply with ONLY valid JSON: {{"score": N, "reason": "brief explanation"}}""",
                temperature=0.3,
                max_tokens=100,
            )

            score = 3
            try:
                parsed = json.loads(eval_response)
                score = int(parsed["score"])
                score = max(1, min(5, score))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                try:
                    if "SCORE:" in eval_response:
                        score_part = eval_response.split("SCORE:")[1].strip()
                        score = int(score_part[0])
                except (IndexError, ValueError):
                    pass

            scores.append(score)
            quality_tests.append({"query": query, "score": score, "note": eval_response[:100]})

        except Exception as e:
            quality_tests.append({"query": query, "score": 0, "note": str(e)})

    results["quality"] = {
        "tests": quality_tests,
        "average_score": sum(scores) / len(scores) if scores else 0,
    }

    return results


# ═══════════════════════════════════════════════════════════════════
# PHASE 6: Cleanup — prune old ingested_items
# ═══════════════════════════════════════════════════════════════════


def phase_6_cleanup() -> dict[str, Any]:
    """Prune ingested_items older than 90 days."""
    results = {"items_pruned": 0, "errors": []}
    try:
        from robothor.memory.ingest_state import cleanup_old_items

        results["items_pruned"] = cleanup_old_items(days=90)
    except Exception as e:
        logger.error("Cleanup failed: %s", e)
        results["errors"].append(str(e))
    return results


# ═══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════


async def get_memory_stats() -> dict[str, Any]:
    """Get current memory system statistics."""
    stats = {}
    try:
        from robothor.memory.facts import get_memory_stats as rag_get_stats

        stats = rag_get_stats()
    except Exception:
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM memory_facts")
            stats["facts_count"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM memory_entities")
            stats["entities_count"] = cur.fetchone()[0]
            cur.close()
            conn.close()
        except Exception as e:
            stats["error"] = str(e)
    return stats


def save_run_report(report: dict[str, Any]):
    """Save the run report to the quality log."""
    quality_log = {"runs": []}

    if QUALITY_LOG.exists():
        try:
            quality_log = json.loads(QUALITY_LOG.read_text())
        except Exception:
            pass

    if "runs" not in quality_log:
        quality_log["runs"] = []

    # Keep last 30 runs
    quality_log["runs"] = quality_log["runs"][-29:] + [report]
    QUALITY_LOG.write_text(json.dumps(quality_log, indent=2, default=str))


async def main():
    """Main deep analysis pipeline (Tier 3)."""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("Deep Analysis Pipeline (Tier 3) Started: %s", start_time)
    logger.info("=" * 60)

    report = {
        "timestamp": start_time.isoformat(),
        "tier": 3,
        "phases": {},
        "stats_before": {},
        "stats_after": {},
        "success": True,
        "duration_seconds": 0,
    }

    # Acquire nightly lock (so Tier 1 continuous_ingest skips)
    lock_fh = None
    try:
        lock_fh = open(NIGHTLY_LOCK, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(start_time.isoformat())
        lock_fh.flush()
    except OSError:
        logger.warning("Could not acquire nightly lock, proceeding anyway")

    try:
        llm_client = await load_llm_client()
        report["stats_before"] = await get_memory_stats()

        # Phase 1: Catch-up retry
        logger.info("Phase 1: Catch-up retry for failed ingestions...")
        report["phases"]["p1_catchup"] = await phase_1_catchup_retry()
        logger.info(
            "  → %d sources retried", report["phases"]["p1_catchup"].get("sources_retried", 0)
        )

        # Phase 2: Relationship Intelligence
        logger.info("Phase 2: Relationship Intelligence...")
        report["phases"]["p2_relationships"] = await phase_2_relationship_intelligence(llm_client)
        logger.info(
            "  → %d briefs", report["phases"]["p2_relationships"].get("briefs_generated", 0)
        )

        # Phase 2.5: Contact Enrichment
        logger.info("Phase 2.5: Contact Enrichment...")
        report["phases"]["p2_5_enrichment"] = await phase_2_5_contact_enrichment(llm_client)
        logger.info(
            "  → %d checked, %d fields updated",
            report["phases"]["p2_5_enrichment"].get("contacts_checked", 0),
            report["phases"]["p2_5_enrichment"].get("fields_updated", 0),
        )

        # Phase 3: Engagement Scoring
        logger.info("Phase 3: Contact Engagement Scoring...")
        report["phases"]["p3_engagement"] = await phase_3_engagement_scoring(llm_client)
        logger.info("  → %d scored", report["phases"]["p3_engagement"].get("scored", 0))

        # Phase 4: Pattern Detection
        logger.info("Phase 4: Cross-System Pattern Detection...")
        report["phases"]["p4_patterns"] = await phase_4_pattern_detection(llm_client)
        logger.info("  → %d patterns", report["phases"]["p4_patterns"].get("patterns_found", 0))

        # Phase 5: Housekeeping
        logger.info("Phase 5: Lifecycle maintenance + quality tests...")
        report["phases"]["p5_housekeeping"] = await phase_5_housekeeping(llm_client)
        logger.info(
            "  → maintenance: %s, quality: %.1f/5",
            report["phases"]["p5_housekeeping"].get("maintenance", {}).get("success", False),
            report["phases"]["p5_housekeeping"].get("quality", {}).get("average_score", 0),
        )

        # Phase 6: Cleanup
        logger.info("Phase 6: Cleanup old ingested_items...")
        report["phases"]["p6_cleanup"] = phase_6_cleanup()
        logger.info("  → %d items pruned", report["phases"]["p6_cleanup"].get("items_pruned", 0))

        report["stats_after"] = await get_memory_stats()

    except Exception as e:
        logger.error("Pipeline error: %s", e)
        report["success"] = False
        report["error"] = str(e)

    finally:
        # Release nightly lock
        if lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except OSError:
                pass

    end_time = datetime.now()
    report["duration_seconds"] = (end_time - start_time).total_seconds()

    save_run_report(report)

    logger.info("=" * 60)
    logger.info("Pipeline Complete: %s (took %.1fs)", end_time, report["duration_seconds"])
    logger.info("=" * 60)

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Deep Analysis Pipeline (Tier 3) — {start_time.date()}")
    print(f"{'=' * 60}")
    print(f"Duration: {report['duration_seconds']:.1f} seconds")
    print(f"Success: {report['success']}")
    print("\nPhases:")
    print(
        f"  Catch-up:      {report['phases'].get('p1_catchup', {}).get('sources_retried', 0)} sources"
    )
    print(
        f"  Rel. Briefs:   {report['phases'].get('p2_relationships', {}).get('briefs_generated', 0)}"
    )
    print(
        f"  Enrichment:    {report['phases'].get('p2_5_enrichment', {}).get('fields_updated', 0)} fields"
    )
    print(f"  Engag. Scores: {report['phases'].get('p3_engagement', {}).get('scored', 0)}")
    print(f"  Patterns:      {report['phases'].get('p4_patterns', {}).get('patterns_found', 0)}")
    print(
        f"  Cleanup:       {report['phases'].get('p6_cleanup', {}).get('items_pruned', 0)} items pruned"
    )
    print(
        f"\nQuality: {report['phases'].get('p5_housekeeping', {}).get('quality', {}).get('average_score', 'N/A')}/5"
    )
    print(f"{'=' * 60}\n")

    return report


if __name__ == "__main__":
    asyncio.run(main())
