"""Telegram self-service onboarding — conversational tenant setup for new users.

When an unregistered Telegram user messages the bot, this module guides them
through a lightweight setup flow, creating a tenant and linking their account.

Uses the existing multi-tenant infrastructure:
- crm.dal.create_tenant_with_user() for tenant + user creation
- memory.blocks.seed_blocks_for_tenant() for memory initialization
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# In-memory state for ongoing onboarding conversations.
# Key: telegram_user_id, Value: onboarding state dict.
_onboarding_sessions: dict[str, dict[str, Any]] = {}

_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")


def is_onboarding(telegram_user_id: str) -> bool:
    """Check if a user has an active onboarding session."""
    return telegram_user_id in _onboarding_sessions


def start_onboarding(telegram_user_id: str) -> str:
    """Begin onboarding and return the first prompt message."""
    _onboarding_sessions[telegram_user_id] = {"step": "name"}
    return (
        "Hi! I don't recognize your account yet. "
        "Let's get you set up — it only takes a moment.\n\n"
        "What's your name?"
    )


def process_onboarding(telegram_user_id: str, user_text: str) -> str | None:
    """Process user input during onboarding.

    Returns:
        The next prompt message, or None if onboarding is complete.
    """
    session = _onboarding_sessions.get(telegram_user_id)
    if not session:
        return None

    step = session.get("step", "name")

    if step == "name":
        name = user_text.strip()
        if not name or len(name) < 2:
            return "I need at least a first name. What should I call you?"
        session["display_name"] = name
        # Auto-generate tenant ID from name
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20]
        session["tenant_id"] = slug
        session["step"] = "confirm"
        return (
            f"Great to meet you, {name}! "
            f"I'll set up your workspace now.\n\n"
            f"Your workspace ID will be: **{slug}**\n\n"
            f"Type **yes** to confirm, or type a different workspace ID."
        )

    if step == "confirm":
        text = user_text.strip().lower()
        if text == "yes":
            return _finalize_onboarding(telegram_user_id, session)
        # User provided a custom tenant ID
        if _TENANT_ID_RE.match(text):
            session["tenant_id"] = text
            return _finalize_onboarding(telegram_user_id, session)
        return (
            "Workspace ID must be lowercase letters, digits, and hyphens "
            "(3-30 chars, start with a letter). Try again, or type **yes** to accept "
            f"**{session['tenant_id']}**."
        )

    # Unknown step — reset
    _cancel_onboarding(telegram_user_id)
    return None


def _finalize_onboarding(telegram_user_id: str, session: dict[str, Any]) -> str:
    """Create tenant + user and clean up onboarding state."""
    tenant_id = session["tenant_id"]
    display_name = session["display_name"]

    try:
        from robothor.crm.dal import create_tenant_with_user, get_tenant

        # Check for collision
        existing = get_tenant(tenant_id)
        if existing:
            _cancel_onboarding(telegram_user_id)
            return (
                f"Workspace ID '{tenant_id}' is already taken. "
                "Please message me again and choose a different one."
            )

        create_tenant_with_user(
            tenant_id=tenant_id,
            display_name=display_name,
            telegram_user_id=telegram_user_id,
            user_display_name=display_name,
        )
        logger.info(
            "Onboarding complete: tenant=%s, user=%s, telegram=%s",
            tenant_id,
            display_name,
            telegram_user_id,
        )
    except Exception as e:
        logger.error("Onboarding failed for %s: %s", telegram_user_id, e)
        _cancel_onboarding(telegram_user_id)
        return f"Something went wrong during setup: {e}\nPlease try again later."

    _cancel_onboarding(telegram_user_id)
    return (
        f"You're all set, {display_name}! Your workspace **{tenant_id}** is ready.\n\n"
        "Try asking me something — I'm here to help."
    )


def _cancel_onboarding(telegram_user_id: str) -> None:
    """Remove onboarding state."""
    _onboarding_sessions.pop(telegram_user_id, None)
