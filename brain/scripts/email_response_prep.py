#!/usr/bin/env python3
"""email_response_prep.py — Enrich email response queue with CRM context.

Runs 6 min before the Email Responder cron. Reads response-queue.json
(written by triage worker), enriches each item with:
  - Full email thread text (via gog CLI)
  - CRM person details (via crm_dal)
  - Top 5 memory facts (via PostgreSQL)
  - Topic-relevant RAG context (via orchestrator)
  - Calendar context (recent/upcoming meetings with contact)
  - CRM conversation history
  - Depth classification (quick vs analytical)
Writes response-inbox.json for the analyst and responder agents.
"""

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

MEMORY_DIR = os.path.expanduser("~/robothor/brain/memory")
QUEUE_PATH = os.path.join(MEMORY_DIR, "response-queue.json")
INBOX_PATH = os.path.join(MEMORY_DIR, "response-inbox.json")
HANDOFF_PATH = os.path.join(MEMORY_DIR, "worker-handoff.json")
EMAIL_LOG_PATH = os.path.join(MEMORY_DIR, "email-log.json")
CALENDAR_LOG_PATH = os.path.join(MEMORY_DIR, "calendar-log.json")


def _get_orchestrator_url():
    try:
        from memory_system.service_registry import get_service_url

        url = get_service_url("orchestrator")
        if url:
            return url
    except ImportError:
        pass
    return "http://localhost:9099"


ORCHESTRATOR_URL = _get_orchestrator_url()

ANALYTICAL_SIGNALS = {
    "report",
    "snapshot",
    "cashflow",
    "proposal",
    "forecast",
    "revenue",
    "budget",
    "deliverable",
    "invoice",
    "margin",
    "pipeline",
    "quarterly",
    "p&l",
    "profit",
    "loss",
}

DB_CONFIG = {
    "dbname": "robothor_memory",
    "user": "philip",
    "host": "/var/run/postgresql",
    "connect_timeout": 3,
}

GOG_ENV = {**os.environ}  # GOG_KEYRING_PASSWORD injected via SOPS at runtime

# CRM DAL
import sys

sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
import crm_dal

STALE_TIMEOUT = timedelta(hours=2)

AUTOMATED_SENDERS = {
    "gemini-notes@google.com",
    "calendar-notification@google.com",
    "noreply@google.com",
    "notifications@github.com",
}


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def extract_email_address(from_field):
    """Extract email address from 'Name <email>' or plain email."""
    if not from_field:
        return None
    match = re.search(r"<([^>]+)>", from_field)
    if match:
        return match.group(1).lower()
    if "@" in from_field:
        return from_field.strip().lower()
    return None


def fetch_email_thread(thread_id):
    """Fetch full email thread text via gog CLI."""
    try:
        result = subprocess.run(
            [
                "gog",
                "gmail",
                "thread",
                "get",
                thread_id,
                "--account",
                "robothor@ironsail.ai",
                "--full",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=GOG_ENV,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def fetch_twenty_person(email_addr):
    """Look up a person in CRM by email via contact_identifiers.

    Returns (crm_data_dict, person_id) tuple.
    """
    if not email_addr:
        return {}, None

    # Find person_id from contact_identifiers
    person_id = None
    try:
        import psycopg2

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT person_id FROM contact_identifiers
            WHERE channel = 'email' AND identifier = %s
            AND person_id IS NOT NULL
            LIMIT 1
        """,
            (email_addr,),
        )
        row = cur.fetchone()
        if row:
            person_id = row[0]
        cur.close()
        conn.close()
    except Exception:
        pass

    if not person_id:
        return {}, None

    # Fetch person details via crm_dal
    try:
        person = crm_dal.get_person(str(person_id))
        if person:
            return {
                "jobTitle": person.get("jobTitle"),
                "city": person.get("city"),
                "company": (person.get("company") or {}).get("name")
                if person.get("company")
                else None,
            }, person_id
    except Exception:
        pass

    return {}, person_id


def fetch_memory_facts(entity_name, limit=5):
    """Fetch top memory facts for an entity from PostgreSQL."""
    if not entity_name:
        return []
    try:
        import psycopg2

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fact_text FROM memory_facts
            WHERE %s = ANY(entities) AND is_active = true
            ORDER BY importance_score DESC
            LIMIT %s
        """,
            (entity_name, limit),
        )
        facts = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return facts
    except Exception:
        return []


def enrich_topic_rag(subject, thread_text, limit=10):
    """Search RAG orchestrator for facts relevant to the email topic."""
    if not subject and not thread_text:
        return []
    query = (subject or "") + " " + (thread_text or "")[:500]
    query = query.strip()
    if not query:
        return []
    try:
        import httpx

        resp = httpx.post(
            f"{ORCHESTRATOR_URL}/search",
            json={"query": query, "limit": limit},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            return [{"text": r.get("text", ""), "score": r.get("score", 0)} for r in results]
    except Exception as e:
        logger.debug("enrich_topic_rag failed: %s", e)
    return []


def enrich_calendar_context(contact_name, email_addr):
    """Find meetings involving this contact in past 14 days / next 7 days."""
    if not contact_name and not email_addr:
        return []
    cal_log = load_json(CALENDAR_LOG_PATH)
    entries = cal_log.get("entries", {})
    now = datetime.now(UTC)
    window_start = now - timedelta(days=14)
    window_end = now + timedelta(days=7)

    matches = []
    name_lower = (contact_name or "").lower()

    for entry in entries.values():
        start_str = entry.get("start")
        if not start_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue

        if not (window_start <= start_dt <= window_end):
            continue

        # Match by attendee email or name in title/attendees
        attendees = entry.get("attendees", [])
        title = entry.get("title", "")
        attendee_strs = " ".join(str(a) for a in attendees).lower()

        matched = False
        if email_addr and email_addr in attendee_strs:
            matched = True
        if name_lower and (name_lower in attendee_strs or name_lower in title.lower()):
            matched = True

        if matched:
            matches.append(
                {
                    "title": title,
                    "start": start_str,
                    "attendees": attendees,
                }
            )

    # Sort by start time, return most recent/upcoming 5
    matches.sort(key=lambda m: m["start"])
    return matches[:5]


def enrich_crm_history(person_id, limit=5):
    """Pull recent CRM conversation summaries for this person."""
    if not person_id:
        return []
    try:
        convos = crm_dal.get_conversations_for_contact(str(person_id))
        results = []
        for c in convos[:limit]:
            results.append(
                {
                    "channel": c.get("inbox_id") or c.get("inboxName", "unknown"),
                    "status": c.get("status", "unknown"),
                    "lastActivity": c.get("lastActivityAt") or c.get("last_activity_at"),
                }
            )
        return results
    except Exception as e:
        logger.debug("enrich_crm_history failed: %s", e)
    return []


def classify_depth(item):
    """Heuristic to tag depth: 'quick' vs 'analytical'."""
    classification = item.get("classification", "")
    if classification == "analytical":
        return "analytical"

    # Check content for analytical signals
    text_to_check = " ".join(
        [
            item.get("subject", ""),
            (item.get("thread") or "")[:3000],
        ]
    ).lower()

    signal_count = 0
    for signal in ANALYTICAL_SIGNALS:
        if signal in text_to_check:
            signal_count += 1

    # Dollar amounts or percentages
    if re.search(r"\$[\d,.]+", text_to_check):
        signal_count += 1
    if re.search(r"\d+%", text_to_check):
        signal_count += 1

    if signal_count >= 2:
        return "analytical"

    # Long thread with at least 1 signal
    thread_len = len(item.get("thread") or "")
    if thread_len > 5000 and signal_count >= 1:
        return "analytical"

    return "quick"


def filter_already_replied(enriched_items):
    """Strip items whose thread already has actionCompletedAt in email-log.

    This is the mechanical guard against the Responder seeing stale items
    that were already replied to by a previous run or the hook pipeline.
    """
    email_log = load_json(EMAIL_LOG_PATH)
    entries = email_log.get("entries", {})
    filtered = []
    for item in enriched_items:
        tid = item.get("threadId")
        if tid and tid in entries and entries[tid].get("actionCompletedAt"):
            continue
        filtered.append(item)
    return filtered


def process_replied_items(inbox_data):
    """Check previous response-inbox.json for replied items and update email-log."""
    replied = [item for item in inbox_data.get("items", []) if item.get("repliedAt")]
    if not replied:
        return

    email_log = load_json(EMAIL_LOG_PATH)
    entries = email_log.get("entries", {})
    updated = False

    for item in replied:
        thread_id = item.get("threadId")
        if thread_id and thread_id in entries:
            entry = entries[thread_id]
            if not entry.get("actionCompletedAt"):
                entry["actionCompletedAt"] = item["repliedAt"]
                updated = True

    if updated:
        save_json(EMAIL_LOG_PATH, email_log)


def move_stale_to_handoff(queue_items):
    """Move items queued >2h without reply to worker-handoff.json."""
    now = datetime.now(UTC)
    stale = []
    remaining = []

    for item in queue_items:
        queued_at = item.get("queuedAt")
        if queued_at:
            try:
                queued_dt = datetime.fromisoformat(queued_at)
                if now - queued_dt > STALE_TIMEOUT:
                    stale.append(item)
                    continue
            except (ValueError, TypeError):
                pass
        remaining.append(item)

    if stale:
        handoff = load_json(HANDOFF_PATH)
        handoff.setdefault("escalations", [])
        for item in stale:
            handoff["escalations"].append(
                {
                    "id": f"stale-resp-{item.get('threadId', 'unknown')}",
                    "source": "email",
                    "sourceId": item.get("threadId"),
                    "reason": "Response queued >2h without reply — auto-escalated",
                    "summary": f'{item.get("from", "?")}: "{item.get("subject", "?")}" — stale in response queue',
                    "urgency": "medium",
                    "handled": False,
                    "createdAt": now.isoformat(),
                    "surfacedAt": None,
                    "resolvedAt": None,
                }
            )
        save_json(HANDOFF_PATH, handoff)

    return remaining


def enrich_item(item):
    """Enrich a single queue item with thread, CRM, facts, topic RAG, calendar, and depth."""
    thread_id = item.get("threadId")

    # 1. Fetch email thread
    item["thread"] = fetch_email_thread(thread_id) if thread_id else None

    # 2. Enrich contact with CRM data
    contact = item.get("contact", {})
    email_addr = extract_email_address(item.get("from"))
    if email_addr:
        contact["email"] = email_addr

    crm_data, person_id = fetch_twenty_person(email_addr)
    if crm_data:
        contact["jobTitle"] = crm_data.get("jobTitle")
        contact["company"] = crm_data.get("company")
        contact["city"] = crm_data.get("city")

    # 3. Fetch memory facts (by contact name)
    entity_name = contact.get("name")
    contact["facts"] = fetch_memory_facts(entity_name)

    item["contact"] = contact
    item["repliedAt"] = None

    # 4. Topic-relevant RAG context
    item["topicContext"] = enrich_topic_rag(item.get("subject"), item.get("thread"))

    # 5. Calendar context (recent/upcoming meetings with this contact)
    item["calendarContext"] = enrich_calendar_context(entity_name, email_addr)

    # 6. CRM conversation history
    item["crmHistory"] = enrich_crm_history(person_id)

    # 7. Depth classification
    item["depth"] = classify_depth(item)

    return item


def main():
    # Process previously replied items
    prev_inbox = load_json(INBOX_PATH)
    process_replied_items(prev_inbox)

    # Load queue
    queue = load_json(QUEUE_PATH)
    queue_items = queue.get("items", [])

    if not queue_items:
        print("response-queue: 0 items")
        # Write empty inbox
        save_json(
            INBOX_PATH,
            {
                "preparedAt": datetime.now(UTC).isoformat(),
                "counts": {"total": 0},
                "items": [],
            },
        )
        return

    # Move stale items to handoff
    queue_items = move_stale_to_handoff(queue_items)

    # Normalize: worker may write 'id' instead of 'threadId'
    for item in queue_items:
        if "id" in item and "threadId" not in item:
            item["threadId"] = item.pop("id")

    # Filter out automated senders
    queue_items = [
        item
        for item in queue_items
        if extract_email_address(item.get("from", "")) not in AUTOMATED_SENDERS
    ]

    # Deduplicate by threadId (worker and cleanup may both route the same email)
    seen_threads = set()
    deduped = []
    for item in queue_items:
        tid = item.get("threadId")
        if tid and tid in seen_threads:
            continue
        if tid:
            seen_threads.add(tid)
        deduped.append(item)
    queue_items = deduped

    # Enrich remaining items
    enriched = []
    for item in queue_items:
        enriched.append(enrich_item(item))

    # Filter out threads that were already replied to
    enriched = filter_already_replied(enriched)

    # Write response-inbox.json
    inbox = {
        "preparedAt": datetime.now(UTC).isoformat(),
        "counts": {"total": len(enriched)},
        "items": enriched,
    }
    save_json(INBOX_PATH, inbox)

    # Clear processed items from queue
    save_json(QUEUE_PATH, {"items": []})

    analytical = sum(1 for i in enriched if i.get("depth") == "analytical")
    print(f"response-inbox: {len(enriched)} items enriched ({analytical} analytical)")


if __name__ == "__main__":
    main()
