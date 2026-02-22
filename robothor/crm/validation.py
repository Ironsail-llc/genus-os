"""
CRM Validation — blocklists, input sanitization, and data quality rules.

All CRM entity creation passes through validation before write.
Blocklists prevent furniture names, bot accounts, and null-like strings
from becoming CRM records.

Usage:
    from robothor.crm.validation import validate_person_input, scrub_null_string

    valid, reason = validate_person_input("Philip", "Ironsail", "philip@example.com")
"""

from __future__ import annotations

# ─── Blocklists ──────────────────────────────────────────────────────────

PERSON_BLOCKLIST: set[str] = {
    # Furniture / objects misidentified as people (from vision pipeline)
    "couch", "chair", "table", "desk", "lamp", "sofa", "bed", "shelf",
    "door", "window", "wall", "floor", "ceiling", "cabinet", "dresser",
    # Bot / system accounts
    "claude", "vision monitor system", "robothor vision monitor",
    "chatwoot inbox monitor", "chatwoot monitor", "robothor system",
    "email responder", "human resources", "gemini (google workspace)",
    "gemini notes", "google meet", "linkedin (automated)",
    "linkedin (noreply)", "gitguardian", "openrouter team",
}

COMPANY_BLOCKLIST: set[str] = {
    "null", "none", "unknown", "test", "n/a",
}

NULL_STRINGS: set[str] = {"null", "none", "n/a"}


def scrub_null_string(value: str | None) -> str | None:
    """Replace literal 'null'/'none'/'n/a' strings with empty string."""
    if value is None:
        return None
    if value.strip().lower() in NULL_STRINGS:
        return ""
    return value


def validate_person_input(
    first_name: str,
    last_name: str = "",
    email: str | None = None,
) -> tuple[bool, str]:
    """Validate person input against blocklist and basic rules.

    Returns:
        (is_valid, reason) tuple.
    """
    full_name = f"{first_name} {last_name}".strip().lower()

    # Blocklist check
    if full_name in PERSON_BLOCKLIST:
        return False, f"blocked: '{full_name}' is in the person blocklist"
    if first_name.strip().lower() in PERSON_BLOCKLIST:
        return False, f"blocked: '{first_name}' is in the person blocklist"

    # Reject literal null strings
    if first_name.strip().lower() in NULL_STRINGS:
        return False, "rejected: first_name is a null-like string"

    # Name too short
    if len(first_name.strip()) < 2:
        return False, "rejected: first_name must be at least 2 characters"

    # Email validation
    if email and "@" not in email:
        return False, "rejected: email must contain '@'"

    return True, "ok"


def validate_company_input(name: str) -> tuple[bool, str]:
    """Validate company name against blocklist.

    Returns:
        (is_valid, reason) tuple.
    """
    if name.strip().lower() in COMPANY_BLOCKLIST:
        return False, f"blocked: '{name}' is in the company blocklist"
    if len(name.strip()) < 2:
        return False, "rejected: company name must be at least 2 characters"
    return True, "ok"


def normalize_email(email: str | None) -> str | None:
    """Normalize email: lowercase, strip whitespace."""
    if not email:
        return None
    normalized = email.lower().strip()
    if "@" not in normalized:
        return None
    return normalized
