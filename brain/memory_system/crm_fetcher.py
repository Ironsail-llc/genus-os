"""
CRM Data Fetcher for Robothor Intelligence Pipeline.

Fetches CRM data via direct SQL (crm_dal) for pipeline analysis.
No AI, no LLM calls — just data retrieval.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# Import crm_dal for native SQL path
sys.path.insert(0, os.path.expanduser("~/robothor/crm/bridge"))
import crm_dal


def fetch_conversations(hours: int = 24) -> list[dict]:
    """Fetch conversations updated in the last N hours with messages."""
    results = crm_dal.fetch_conversations_for_ingestion(hours)
    logger.info("Fetched %d conversations via SQL (last %dh)", len(results), hours)
    return results


def fetch_twenty_contacts(hours: int = 24) -> list[dict]:
    """Fetch recently updated people.

    Returns list of dicts with: id, firstName, lastName, email, phone,
    jobTitle, company, city, updatedAt.
    """
    results = crm_dal.fetch_contacts_for_ingestion(hours)
    logger.info("Fetched %d contacts via SQL (last %dh)", len(results), hours)
    return results


def fetch_twenty_notes(hours: int = 24) -> list[dict]:
    """Fetch recent notes.

    Returns list of dicts with: id, title, body, createdAt, updatedAt, targets.
    """
    results = crm_dal.fetch_notes_for_ingestion(hours)
    logger.info("Fetched %d notes via SQL (last %dh)", len(results), hours)
    return results


def fetch_twenty_tasks(hours: int = 24) -> list[dict]:
    """Fetch recent tasks.

    Returns list of dicts with: id, title, body, status, dueAt, updatedAt, targets.
    """
    results = crm_dal.fetch_tasks_for_ingestion(hours)
    logger.info("Fetched %d tasks via SQL (last %dh)", len(results), hours)
    return results


def fetch_all_contacts() -> list[dict]:
    """Fetch all people for relationship analysis."""
    results = crm_dal.fetch_all_contacts_for_ingestion()
    logger.info("Fetched %d contacts via SQL (all)", len(results))
    return results


def format_conversation_for_ingestion(conv: dict) -> str:
    """Format a conversation as a string for fact extraction."""
    lines = [
        f"Conversation #{conv['id']} with {conv['contact_name']}",
        f"Contact Email: {conv.get('contact_email', 'unknown')}",
        f"Status: {conv['status']}",
        f"Inbox: {conv.get('inbox_name', '')}",
        "",
    ]
    for msg in conv.get("messages", [])[-10:]:  # last 10 messages
        direction = "→" if msg.get("type") == 1 else "←"
        if msg.get("private"):
            direction = "🔒"
        lines.append(f"  {direction} {msg.get('sender', '?')}: {msg.get('content', '')[:500]}")

    return "\n".join(lines)


def format_contact_for_ingestion(contact: dict) -> str:
    """Format a CRM contact as a string for fact extraction."""
    parts = [f"Contact: {contact['firstName']} {contact['lastName']}"]
    if contact.get("email"):
        parts.append(f"Email: {contact['email']}")
    if contact.get("phone"):
        parts.append(f"Phone: {contact['phone']}")
    if contact.get("jobTitle"):
        parts.append(f"Title: {contact['jobTitle']}")
    if contact.get("company"):
        parts.append(f"Company: {contact['company']}")
    if contact.get("city"):
        parts.append(f"City: {contact['city']}")
    return "\n".join(parts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    convs = fetch_conversations(168)
    contacts = fetch_all_contacts()
    notes = fetch_twenty_notes(168)
    tasks = fetch_twenty_tasks(168)
    print(f"Conversations: {len(convs)}")
    print(f"Contacts: {len(contacts)}")
    print(f"Notes: {len(notes)}")
    print(f"Tasks: {len(tasks)}")
    if contacts:
        print(f"  Sample: {contacts[0]['firstName']} {contacts[0]['lastName']}")
