#!/usr/bin/env python3
"""
Email Processing — Helper module for email cron.

This module provides:
    - Email fetching via gog CLI
    - Decision structures for email processing
    - Log update functions

The actual processing is done by an agent cron that uses this module.
This allows us to test the decision logic and log formats.

Usage by agent cron:
    from email_processing import (
        fetch_new_emails,
        EmailDecision,
        update_email_log,
        get_context_for_email,
    )
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from cron_context import (
    CronContext,
    load_json,
    save_json,
)

# === Paths ===
MEMORY_DIR = Path("/home/philip/clawd/memory")
EMAIL_LOG = MEMORY_DIR / "email-log.json"
CONTACTS_FILE = MEMORY_DIR / "contacts.json"

# Config
EMAIL_ACCOUNT = "robothor@ironsail.ai"
GOG_PASSWORD = os.environ["GOG_KEYRING_PASSWORD"]


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ResponseType(str, Enum):
    NONE = "none"  # No response needed
    AUTO = "auto"  # Can respond automatically
    DRAFT = "draft"  # Draft for Philip's review
    ESCALATE = "escalate"  # Needs Philip's decision


@dataclass
class EmailDecision:
    """Decision structure for processing an email."""

    email_id: str
    urgency: Urgency = Urgency.LOW
    needs_response: bool = False
    response_type: ResponseType = ResponseType.NONE
    needs_action: bool = False
    action_owner: str = ""
    summary: str = ""
    escalate_reason: str | None = None
    related_meeting_id: str | None = None
    related_task_id: str | None = None
    suggested_response: str | None = None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "urgency": self.urgency.value if isinstance(self.urgency, Urgency) else self.urgency,
            "needs_response": self.needs_response,
            "response_type": self.response_type.value
            if isinstance(self.response_type, ResponseType)
            else self.response_type,
            "needs_action": self.needs_action,
            "action_owner": self.action_owner,
            "summary": self.summary,
            "escalate_reason": self.escalate_reason,
            "related_meeting_id": self.related_meeting_id,
            "related_task_id": self.related_task_id,
            "suggested_response": self.suggested_response,
        }


def fetch_new_emails(max_results: int = 20) -> list[dict]:
    """Fetch unread emails via gog CLI."""
    try:
        env = os.environ.copy()
        env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD

        result = subprocess.run(
            [
                "gog",
                "gmail",
                "search",
                "is:unread",
                "--account",
                EMAIL_ACCOUNT,
                "--max",
                str(max_results),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        if result.returncode != 0:
            print(f"[ERROR] gog gmail failed: {result.stderr}")
            return []

        try:
            data = json.loads(result.stdout)
            if isinstance(data, dict) and "messages" in data:
                return data["messages"]
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError:
            return []

    except Exception as e:
        print(f"[ERROR] Failed to fetch emails: {e}")
        return []


def get_email_details(email_id: str) -> dict | None:
    """Fetch full email details."""
    try:
        env = os.environ.copy()
        env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD

        result = subprocess.run(
            ["gog", "gmail", "get", email_id, "--account", EMAIL_ACCOUNT, "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        return json.loads(result.stdout)
    except:
        return None


def mark_as_read(email_id: str) -> bool:
    """Mark email as read."""
    try:
        env = os.environ.copy()
        env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD

        result = subprocess.run(
            [
                "gog",
                "gmail",
                "thread",
                "modify",
                email_id,
                "--account",
                EMAIL_ACCOUNT,
                "--remove",
                "UNREAD",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

        return result.returncode == 0
    except:
        return False


def get_context_for_email(email: dict, ctx: CronContext) -> dict:
    """
    Build context for processing an email.

    Returns relevant context from other logs that might help
    the agent understand and respond to this email.
    """
    sender = email.get("from", "")
    subject = email.get("subject", "")

    context = {
        "sender_contact": None,
        "related_meetings": [],
        "related_tasks": [],
        "recent_thread": [],
    }

    # Find sender in contacts
    context["sender_contact"] = ctx.contacts.find_by_email(sender)

    # Find related meetings (same attendees or subject match)
    subject_lower = subject.lower()
    for meeting in ctx.calendar.upcoming:
        if sender in meeting.get("attendees", []) or any(
            word in meeting.get("title", "").lower()
            for word in subject_lower.split()
            if len(word) > 4
        ):
            context["related_meetings"].append(meeting)

    # Find related tasks
    for task in ctx.tasks.pending:
        if any(
            word in task.get("description", "").lower()
            for word in subject_lower.split()
            if len(word) > 4
        ):
            context["related_tasks"].append(task)

    return context


def update_email_log(email: dict, decision: EmailDecision) -> str:
    """
    Update email-log.json with processed email.

    Returns the email ID.
    """
    now = datetime.now().isoformat()

    email_log = load_json(EMAIL_LOG, {"entries": {}})

    email_id = email.get("id", "")

    entry = {
        "id": email_id,
        "from": email.get("from", ""),
        "to": email.get("to", ""),
        "subject": email.get("subject", ""),
        "receivedAt": email.get("date", email.get("internalDate", now)),
        "processedAt": now,
        "threadId": email.get("threadId", ""),
        # Decision fields
        **decision.to_dict(),
        # Status fields
        "surfacedAt": None,
        "reviewedAt": None,
        "respondedAt": None,
        "escalated": decision.response_type == ResponseType.ESCALATE,
    }

    email_log["entries"][email_id] = entry
    email_log["lastProcessedAt"] = now

    save_json(EMAIL_LOG, email_log)

    return email_id


def update_contact(email: dict, ctx: CronContext) -> None:
    """Update contacts with sender info if new."""
    sender = email.get("from", "")
    if not sender:
        return

    # Extract email and name
    match = re.match(r"^(.+?)\s*<(.+?)>$", sender)
    if match:
        name, email_addr = match.groups()
    else:
        name = ""
        email_addr = sender

    email_addr = email_addr.lower().strip()

    # Check if already in contacts
    if ctx.contacts.find_by_email(email_addr):
        return

    # Add to contacts
    contacts_data = load_json(CONTACTS_FILE, {"contacts": {}})

    contact_id = email_addr.replace("@", "_at_").replace(".", "_")
    contacts_data["contacts"][contact_id] = {
        "email": email_addr,
        "name": name.strip(),
        "firstSeen": datetime.now().isoformat(),
        "source": "email",
        "notes": "",
    }

    save_json(CONTACTS_FILE, contacts_data)


# === Urgency Heuristics (can be used by agent or standalone) ===

CRITICAL_KEYWORDS = [
    "urgent",
    "emergency",
    "critical",
    "asap",
    "immediately",
    "security",
    "breach",
    "hack",
    "down",
    "outage",
    "prescription",
    "patient",
    "healthcare",
]

HIGH_KEYWORDS = [
    "important",
    "deadline",
    "today",
    "eod",
    "priority",
    "payment",
    "invoice",
    "legal",
    "contract",
]

LOW_SENDERS = [
    "newsletter",
    "noreply",
    "notifications",
    "marketing",
    "updates@",
    "info@",
    "support@",
]


def estimate_urgency(email: dict) -> Urgency:
    """
    Estimate urgency based on keywords and sender.

    This is a heuristic - the agent can override with better judgment.
    """
    subject = (email.get("subject", "") or "").lower()
    sender = (email.get("from", "") or "").lower()
    body = (email.get("snippet", "") or "").lower()
    text = f"{subject} {body}"

    # Check for low-priority senders first
    for pattern in LOW_SENDERS:
        if pattern in sender:
            return Urgency.LOW

    # Check for critical keywords
    for keyword in CRITICAL_KEYWORDS:
        if keyword in text:
            return Urgency.CRITICAL

    # Check for high keywords
    for keyword in HIGH_KEYWORDS:
        if keyword in text:
            return Urgency.HIGH

    # Default to medium for actual correspondence
    if "@" in sender and "noreply" not in sender:
        return Urgency.MEDIUM

    return Urgency.LOW


def needs_response_heuristic(email: dict) -> bool:
    """
    Estimate if email needs a response.

    This is a heuristic - the agent can override.
    """
    subject = (email.get("subject", "") or "").lower()
    body = (email.get("snippet", "") or "").lower()
    text = f"{subject} {body}"

    # Questions usually need responses
    if "?" in text:
        return True

    # Action requests
    action_phrases = [
        "can you",
        "could you",
        "please",
        "would you",
        "let me know",
        "get back to",
        "reply",
        "respond",
        "send me",
        "share",
        "update",
    ]
    for phrase in action_phrases:
        if phrase in text:
            return True

    return False


if __name__ == "__main__":
    # Quick test
    print("Testing email processing module...")

    ctx = CronContext.load()
    print(f"Context loaded: {ctx.summary()}")

    emails = fetch_new_emails(max_results=5)
    print(f"Fetched {len(emails)} unread emails")

    for email in emails[:3]:
        urgency = estimate_urgency(email)
        needs_resp = needs_response_heuristic(email)
        print(f"  [{urgency.value}] {email.get('subject', 'No subject')[:50]}")
        print(f"       From: {email.get('from', '?')[:40]}, Needs response: {needs_resp}")
