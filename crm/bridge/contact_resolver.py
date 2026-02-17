"""Cross-system contact resolution.

Maps any channel identifier to a unified contact across:
- contact_identifiers table (robothor_memory)
- Twenty CRM (people)
- Chatwoot (contacts)
- Memory system (entities)
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import httpx
import config
import twenty_client
import chatwoot_client


def get_db():
    return psycopg2.connect(config.PG_DSN)


async def resolve(channel: str, identifier: str, name: str | None = None,
                  client: httpx.AsyncClient = None) -> dict:
    """Resolve a channel identifier to cross-system IDs.

    Returns dict with twenty_person_id, chatwoot_contact_id, memory_entity_id.
    Creates records in any system where the contact doesn't exist yet.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Check if we already have a mapping
    cur.execute(
        "SELECT * FROM contact_identifiers WHERE channel = %s AND identifier = %s",
        (channel, identifier)
    )
    existing = cur.fetchone()

    if existing and existing["twenty_person_id"] and existing["chatwoot_contact_id"]:
        conn.close()
        return dict(existing)

    # If not found or incomplete, try to fill in gaps
    twenty_id = existing["twenty_person_id"] if existing else None
    chatwoot_id = existing["chatwoot_contact_id"] if existing else None
    entity_id = existing["memory_entity_id"] if existing else None
    display = name or (existing["display_name"] if existing else identifier)

    # Resolve in Twenty
    if not twenty_id and client:
        search_term = name or identifier
        people = await twenty_client.search_people(search_term, client)
        if people:
            twenty_id = people[0]["id"]
        elif name:
            parts = name.split(None, 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""
            email = identifier if channel == "email" else None
            phone = identifier if channel in ("voice", "sms") else None
            twenty_id = await twenty_client.create_person(first, last, email, phone, client)

    # Resolve in Chatwoot
    if not chatwoot_id and client:
        contacts = await chatwoot_client.search_contacts(name or identifier, client)
        if contacts:
            chatwoot_id = contacts[0]["id"]
        else:
            email = identifier if channel == "email" else None
            phone = identifier if channel in ("voice", "sms") else None
            contact = await chatwoot_client.create_contact(
                display, email=email, phone=phone,
                identifier=f"{channel}:{identifier}", client=client
            )
            if contact:
                chatwoot_id = contact.get("id")

    # Upsert the mapping
    cur.execute("""
        INSERT INTO contact_identifiers (channel, identifier, display_name, twenty_person_id, chatwoot_contact_id, memory_entity_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (channel, identifier) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, contact_identifiers.display_name),
            twenty_person_id = COALESCE(EXCLUDED.twenty_person_id, contact_identifiers.twenty_person_id),
            chatwoot_contact_id = COALESCE(EXCLUDED.chatwoot_contact_id, contact_identifiers.chatwoot_contact_id),
            memory_entity_id = COALESCE(EXCLUDED.memory_entity_id, contact_identifiers.memory_entity_id),
            updated_at = NOW()
        RETURNING *
    """, (channel, identifier, display, twenty_id, chatwoot_id, entity_id))
    result = cur.fetchone()
    conn.commit()
    conn.close()

    return dict(result) if result else {}


async def get_timeline(identifier: str, client: httpx.AsyncClient) -> dict:
    """Get unified timeline for a contact across all systems."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Find all identifiers for this person
    cur.execute(
        "SELECT * FROM contact_identifiers WHERE identifier = %s OR display_name ILIKE %s",
        (identifier, f"%{identifier}%")
    )
    mappings = cur.fetchall()
    conn.close()

    timeline = {"identifier": identifier, "mappings": [dict(m) for m in mappings], "twenty": None, "chatwoot_conversations": [], "memory_facts": []}

    if not mappings:
        return timeline

    # Get Twenty data
    for m in mappings:
        if m.get("twenty_person_id"):
            person = await twenty_client.get_person(m["twenty_person_id"], client)
            if person:
                timeline["twenty"] = person
                break

    # Get Chatwoot conversations
    for m in mappings:
        if m.get("chatwoot_contact_id"):
            convos = await chatwoot_client.get_conversations(m["chatwoot_contact_id"], client)
            timeline["chatwoot_conversations"] = convos
            break

    # Get memory facts
    try:
        r = await client.get(f"{config.MEMORY_URL}/search", params={"query": identifier, "limit": 10})
        if r.status_code == 200:
            timeline["memory_facts"] = r.json().get("results", [])
    except Exception:
        pass

    return timeline
