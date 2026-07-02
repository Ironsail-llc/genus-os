"""
CRM Validation — blocklists, input sanitization, and data quality rules.

All CRM entity creation passes through validation before write.
Blocklists prevent furniture names, bot accounts, and null-like strings
from becoming CRM records.

Usage:
    from robothor.crm.validation import validate_person_input, scrub_null_string

    valid, reason = validate_person_input("Jane", "Smith", "jane@example.com")
"""

from __future__ import annotations

from typing import Any

# ─── Blocklists ──────────────────────────────────────────────────────────

PERSON_BLOCKLIST: set[str] = {
    # Furniture / objects misidentified as people (from vision pipeline)
    "couch",
    "chair",
    "table",
    "desk",
    "lamp",
    "sofa",
    "bed",
    "shelf",
    "door",
    "window",
    "wall",
    "floor",
    "ceiling",
    "cabinet",
    "dresser",
    # Bot / system accounts
    "claude",
    "vision monitor system",
    "robothor vision monitor",
    "chatwoot inbox monitor",
    "chatwoot monitor",
    "robothor system",
    "email responder",
    "human resources",
    "gemini (google workspace)",
    "gemini notes",
    "google meet",
    "linkedin (automated)",
    "linkedin (noreply)",
    "gitguardian",
    "openrouter team",
}

COMPANY_BLOCKLIST: set[str] = {
    "null",
    "none",
    "unknown",
    "test",
    "n/a",
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


# ─── Autonomy budget validation ───────────────────────────────────────────
# Lives here (not in robothor.engine.autonomy) so the CRM data layer can
# validate the autonomy_budget JSONB it persists without importing the engine.
# Engine code keeps `from robothor.engine.autonomy import validate_budget`
# working via a re-export there.

# Allowed top-level keys in an autonomy_budget dict. Extra keys are typos, not
# features — the validator rejects them so the planner never silently degrades.
_VALID_BUDGET_KEYS = frozenset(
    {
        "reversible_cap_usd",
        "irreversible_cap_usd",
        "categories",
        "hard_floor",
    }
)

_VALID_VERDICTS = frozenset({"auto", "ask", "refuse"})


def validate_budget(budget: Any) -> tuple[bool, str]:
    """Validate an autonomy_budget dict before persisting to JSONB.

    Returns ``(True, "")`` for empty dicts and partial-but-recognized shapes —
    legacy rows must continue to round-trip cleanly. Returns ``(False, reason)``
    for clearly malformed inputs the DAL should reject with ``{"error": reason}``.

    Recognized shape:
        {
          "reversible_cap_usd":   <non-negative number>,
          "irreversible_cap_usd": <non-negative number>,
          "categories":           {<action_type>: "auto"|"ask"|"refuse"},
          "hard_floor":           [<action_type>, ...],
        }
    """
    if not isinstance(budget, dict):
        return False, "autonomy_budget must be a dict"

    extra = set(budget.keys()) - _VALID_BUDGET_KEYS
    if extra:
        return False, f"unknown autonomy_budget key(s): {sorted(extra)}"

    for cap_key in ("reversible_cap_usd", "irreversible_cap_usd"):
        if cap_key in budget:
            cap = budget[cap_key]
            if isinstance(cap, bool) or not isinstance(cap, (int, float)):
                return False, f"{cap_key} must be a non-negative number"
            if cap < 0:
                return False, f"{cap_key} must be non-negative (got {cap})"

    if "categories" in budget:
        cats = budget["categories"]
        if not isinstance(cats, dict):
            return False, "categories must be a dict of action_type → verdict"
        for action_type, verdict in cats.items():
            if not isinstance(action_type, str):
                return False, f"categories keys must be strings (got {type(action_type).__name__})"
            if verdict not in _VALID_VERDICTS:
                return (
                    False,
                    f"categories[{action_type!r}] must be one of {sorted(_VALID_VERDICTS)} (got {verdict!r})",
                )

    if "hard_floor" in budget:
        hf = budget["hard_floor"]
        if not isinstance(hf, list):
            return False, "hard_floor must be a list of action_type strings"
        for entry in hf:
            if not isinstance(entry, str):
                return False, f"hard_floor entries must be strings (got {type(entry).__name__})"

    return True, ""
