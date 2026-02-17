#!/home/philip/robothor/crm/bridge/venv/bin/python3
"""Chatwoot Backfill — Sync contacts and historical conversations.

Phase A: Sync contacts from contacts.json + contact_id_map.json → Chatwoot
Phase B: Import email threads from email-log.json → Chatwoot conversations
Phase C: (optional) Import transcripts from short_term_memory → Chatwoot

Usage:
    python3 backfill_chatwoot.py                  # Phase A + B
    python3 backfill_chatwoot.py --dry-run        # Print only
    python3 backfill_chatwoot.py --transcripts    # Phase A + B + C
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

# ── Config ──────────────────────────────────────────────────

CHATWOOT_URL = "http://localhost:3100"
CHATWOOT_API_TOKEN = "X9PstchkkPW4ViY8rTPh8vkt"
CHATWOOT_ACCOUNT_ID = 1
CHATWOOT_INBOX_ID = 2  # Robothor Bridge API inbox
PG_DSN = "dbname=robothor_memory user=philip host=/var/run/postgresql"

CONTACTS_JSON = Path("/home/philip/clawd/memory/contacts.json")
CONTACT_ID_MAP = Path("/home/philip/robothor/crm/contact_id_map.json")
EMAIL_LOG = Path("/home/philip/clawd/memory/email-log.json")

BASE = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"
HEADERS = {"api_access_token": CHATWOOT_API_TOKEN, "Content-Type": "application/json"}

RATE_LIMIT_MS = 100

# Skip these "contacts" — they're automated systems, not people
SKIP_CONTACTS = {
    "Google Calendar", "Google Workspace", "Jira Automation",
    "Garmin Connect", "System Cron", "Robothor",
    "Twilio Notifications", "ngrok Team", "Arc Notifications",
}


def sleep_rate():
    time.sleep(RATE_LIMIT_MS / 1000)


# ── Chatwoot API helpers ────────────────────────────────────

async def cw_search_contacts(query: str, client: httpx.AsyncClient) -> list:
    r = await client.get(f"{BASE}/contacts/search", params={"q": query}, headers=HEADERS)
    if r.status_code == 200:
        return r.json().get("payload", [])
    return []


async def cw_create_contact(name: str, email: str | None = None,
                            phone: str | None = None, identifier: str | None = None,
                            client: httpx.AsyncClient = None) -> dict | None:
    data = {"name": name, "inbox_id": CHATWOOT_INBOX_ID}
    if email:
        data["email"] = email
    if phone:
        data["phone_number"] = phone
    if identifier:
        data["identifier"] = identifier
    r = await client.post(f"{BASE}/contacts", json=data, headers=HEADERS)
    if r.status_code in (200, 201):
        payload = r.json()
        contact = payload.get("payload", {}).get("contact", payload)
        return contact
    return None


async def cw_create_conversation(contact_id: int, client: httpx.AsyncClient) -> dict | None:
    r = await client.post(f"{BASE}/conversations", json={
        "contact_id": contact_id,
        "inbox_id": CHATWOOT_INBOX_ID,
    }, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json()
    return None


async def cw_send_message(conversation_id: int, content: str, message_type: str = "incoming",
                          client: httpx.AsyncClient = None) -> dict | None:
    r = await client.post(f"{BASE}/conversations/{conversation_id}/messages", json={
        "content": content,
        "message_type": message_type,
    }, headers=HEADERS)
    if r.status_code in (200, 201):
        return r.json()
    return None


# ── Phase A: Contact Sync ──────────────────────────────────

async def phase_a(dry_run: bool, client: httpx.AsyncClient) -> dict:
    """Sync contacts from contacts.json → Chatwoot. Returns updated id map."""
    print("\n=== Phase A: Contact Sync ===")

    contacts_data = json.loads(CONTACTS_JSON.read_text())
    contacts = contacts_data.get("contacts", [])
    id_map = json.loads(CONTACT_ID_MAP.read_text())

    created = 0
    skipped = 0
    already = 0

    for contact in contacts:
        name = contact.get("name", "")
        if name in SKIP_CONTACTS or not name:
            skipped += 1
            continue

        email = contact.get("email")
        phone = contact.get("phone")
        map_entry = id_map.get(name, {})

        # Check if already has chatwoot_contact_id
        if map_entry.get("chatwoot_contact_id"):
            already += 1
            continue

        # Search Chatwoot by email first (dedup)
        if email:
            existing = await cw_search_contacts(email, client)
            if existing:
                cw_id = existing[0]["id"]
                print(f"  Found existing: {name} → Chatwoot #{cw_id}")
                if name in id_map:
                    id_map[name]["chatwoot_contact_id"] = cw_id
                already += 1
                sleep_rate()
                continue

        # Search by name
        existing = await cw_search_contacts(name, client)
        if existing:
            cw_id = existing[0]["id"]
            print(f"  Found existing: {name} → Chatwoot #{cw_id}")
            if name in id_map:
                id_map[name]["chatwoot_contact_id"] = cw_id
            already += 1
            sleep_rate()
            continue

        # Create new contact
        if dry_run:
            print(f"  [DRY RUN] Would create: {name} ({email})")
            created += 1
            continue

        result = await cw_create_contact(name, email=email, phone=phone,
                                         identifier=f"crm:{name}", client=client)
        if result:
            cw_id = result.get("id")
            print(f"  Created: {name} → Chatwoot #{cw_id}")
            if name in id_map:
                id_map[name]["chatwoot_contact_id"] = cw_id

            # Register in contact_identifiers table
            twenty_id = map_entry.get("twenty_person_id")
            _upsert_contact_identifier(name, email, twenty_id, cw_id)

            created += 1
        else:
            print(f"  FAILED to create: {name}")

        sleep_rate()

    # Save updated map
    if not dry_run:
        CONTACT_ID_MAP.write_text(json.dumps(id_map, indent=2) + "\n")
        print(f"\n  Updated {CONTACT_ID_MAP}")

    print(f"\n  Phase A complete: {created} created, {already} existing, {skipped} skipped")
    return id_map


def _upsert_contact_identifier(name: str, email: str | None,
                                twenty_id: str | None, chatwoot_id: int | None):
    """Insert into contact_identifiers table."""
    try:
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        channel = "email" if email else "crm"
        identifier = email or name
        cur.execute("""
            INSERT INTO contact_identifiers (channel, identifier, display_name, twenty_person_id, chatwoot_contact_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (channel, identifier) DO UPDATE SET
                twenty_person_id = COALESCE(EXCLUDED.twenty_person_id, contact_identifiers.twenty_person_id),
                chatwoot_contact_id = COALESCE(EXCLUDED.chatwoot_contact_id, contact_identifiers.chatwoot_contact_id),
                updated_at = NOW()
        """, (channel, identifier, name, twenty_id, chatwoot_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  Warning: DB upsert failed for {name}: {e}")


# ── Phase B: Email Thread Import ───────────────────────────

def extract_email(s: str | None) -> str | None:
    """Extract bare email from 'Name <email>' or plain email string."""
    if not s:
        return None
    m = re.search(r"<([^>]+@[^>]+)>", s)
    if m:
        return m.group(1).strip()
    if "@" in s:
        return s.strip()
    return None


def normalize_subject(subject: str | None) -> str:
    """Strip Re:/Fwd: prefixes and normalize whitespace."""
    if not subject:
        return "(no subject)"
    s = re.sub(r"^(Re|Fwd|Fw)\s*:\s*", "", subject, flags=re.IGNORECASE)
    s = re.sub(r"^(Re|Fwd|Fw)\s*:\s*", "", s, flags=re.IGNORECASE)  # double strip
    return s.strip() or "(no subject)"


async def phase_b(dry_run: bool, id_map: dict, client: httpx.AsyncClient):
    """Import email threads → Chatwoot conversations."""
    print("\n=== Phase B: Email Thread Import ===")

    email_data = json.loads(EMAIL_LOG.read_text())
    entries = email_data.get("entries", {})

    # Build sender → emails list, grouped by sender + normalized subject
    threads: dict[str, list] = {}  # key: "sender|normalized_subject"

    for eid, entry in entries.items():
        sender = entry.get("from") or "Unknown"
        subject = entry.get("subject")
        norm_subj = normalize_subject(subject)

        # Determine the "contact" — the person who isn't Robothor
        sender_email = extract_email(sender)
        to_list = entry.get("to") or []
        if sender_email == "robothor@ironsail.ai":
            # Outgoing email — contact is the first recipient that isn't robothor
            contact_email = None
            for t in to_list:
                e = extract_email(t)
                if e and e != "robothor@ironsail.ai":
                    contact_email = e
                    break
        else:
            # Incoming email — contact is the sender
            contact_email = sender_email

        if not contact_email:
            continue

        thread_key = f"{contact_email}|{norm_subj}"
        if thread_key not in threads:
            threads[thread_key] = []
        threads[thread_key].append(entry)

    # Sort each thread by receivedAt
    for key in threads:
        threads[key].sort(key=lambda e: e.get("receivedAt", ""))

    print(f"  Found {len(threads)} email threads from {len(entries)} emails")

    # Reverse lookup: email → contact name → chatwoot_contact_id
    email_to_contact = {}
    for name, info in id_map.items():
        if info.get("email"):
            email_to_contact[info["email"].lower()] = (name, info.get("chatwoot_contact_id"))

    # Also check contacts.json for alt emails
    contacts_data = json.loads(CONTACTS_JSON.read_text())
    for contact in contacts_data.get("contacts", []):
        cname = contact.get("name", "")
        for alt in contact.get("altEmails", []):
            if alt.lower() not in email_to_contact and cname in id_map:
                email_to_contact[alt.lower()] = (cname, id_map[cname].get("chatwoot_contact_id"))

    created_convos = 0
    created_msgs = 0
    skipped = 0

    for thread_key, messages in threads.items():
        contact_email = thread_key.split("|", 1)[0]
        norm_subj = thread_key.split("|", 1)[1]

        lookup = email_to_contact.get(contact_email.lower())
        if not lookup or not lookup[1]:
            skipped += 1
            continue

        contact_name, chatwoot_contact_id = lookup

        if dry_run:
            print(f"  [DRY RUN] Thread: {contact_name} — \"{norm_subj}\" ({len(messages)} msgs)")
            created_convos += 1
            created_msgs += len(messages)
            continue

        # Create conversation
        convo = await cw_create_conversation(chatwoot_contact_id, client)
        if not convo:
            print(f"  FAILED: conversation for {contact_name}")
            continue

        convo_id = convo.get("id")
        created_convos += 1
        sleep_rate()

        # Add messages chronologically
        for entry in messages:
            sender_raw = entry.get("from", "")
            sender_addr = extract_email(sender_raw)
            subject = entry.get("subject", "(no subject)")
            summary = entry.get("summary", "")
            received = entry.get("receivedAt", "")

            content = f"**Subject:** {subject}\n\n{summary}\n\n*{received}*"
            direction = "outgoing" if sender_addr == "robothor@ironsail.ai" else "incoming"

            await cw_send_message(convo_id, content, direction, client)
            created_msgs += 1
            sleep_rate()

        print(f"  Created: {contact_name} — \"{norm_subj}\" → convo #{convo_id} ({len(messages)} msgs)")

    print(f"\n  Phase B complete: {created_convos} conversations, {created_msgs} messages, {skipped} skipped (no Chatwoot contact)")


# ── Phase C: Transcript Import ─────────────────────────────

async def phase_c(dry_run: bool, id_map: dict, client: httpx.AsyncClient):
    """Import transcripts from short_term_memory → Chatwoot."""
    print("\n=== Phase C: Transcript Import ===")

    # Philip's Chatwoot contact ID
    philip_info = id_map.get("Philip D'Agostino", {})
    philip_cw_id = philip_info.get("chatwoot_contact_id")
    if not philip_cw_id:
        print("  ERROR: Philip not found in Chatwoot — run Phase A first")
        return

    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Get sessions from short_term_memory
    cur.execute("""
        SELECT session_id, array_agg(
            json_build_object('role', role, 'content', content, 'ts', created_at)
            ORDER BY created_at
        ) as messages
        FROM short_term_memory
        WHERE session_id IS NOT NULL
          AND role IN ('transcript_user', 'transcript_assistant')
        GROUP BY session_id
        ORDER BY min(created_at)
    """)
    sessions = cur.fetchall()
    conn.close()

    print(f"  Found {len(sessions)} transcript sessions")

    created_convos = 0
    created_msgs = 0

    for session in sessions:
        session_id = session["session_id"]
        messages = session["messages"]

        if dry_run:
            print(f"  [DRY RUN] Session {session_id[:12]}... ({len(messages)} msgs)")
            created_convos += 1
            created_msgs += len(messages)
            continue

        convo = await cw_create_conversation(philip_cw_id, client)
        if not convo:
            print(f"  FAILED: conversation for session {session_id[:12]}...")
            continue

        convo_id = convo.get("id")
        created_convos += 1
        sleep_rate()

        for msg in messages:
            content = msg.get("content", "")
            if not content:
                continue
            role = msg.get("role", "")
            direction = "incoming" if role == "transcript_user" else "outgoing"
            await cw_send_message(convo_id, content, direction, client)
            created_msgs += 1
            sleep_rate()

        print(f"  Session {session_id[:12]}... → convo #{convo_id} ({len(messages)} msgs)")

    print(f"\n  Phase C complete: {created_convos} conversations, {created_msgs} messages")


# ── Main ───────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Backfill Chatwoot with contacts and conversations")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without making changes")
    parser.add_argument("--transcripts", action="store_true", help="Include Phase C (transcript import)")
    parser.add_argument("--phase", choices=["a", "b", "c"], help="Run only a specific phase")
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY RUN MODE — no changes will be made]")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Verify Chatwoot is reachable
        try:
            r = await client.get(f"{CHATWOOT_URL}/api")
            if r.status_code != 200:
                print(f"ERROR: Chatwoot unreachable (HTTP {r.status_code})")
                sys.exit(1)
        except Exception as e:
            print(f"ERROR: Chatwoot unreachable: {e}")
            sys.exit(1)

        id_map = json.loads(CONTACT_ID_MAP.read_text())

        if args.phase == "a" or not args.phase:
            id_map = await phase_a(args.dry_run, client)

        if args.phase == "b" or not args.phase:
            await phase_b(args.dry_run, id_map, client)

        if args.phase == "c" or args.transcripts:
            await phase_c(args.dry_run, id_map, client)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
