"""Per-user tenant resolution for multi-user Telegram routing.

Maps Telegram user IDs to tenants via the ``tenant_users`` database table.
Results are cached in-process for fast repeated lookups.

Usage::

    from robothor.engine.users import lookup_user

    info = lookup_user("123456789")
    # -> {"tenant_id": "acme", "display_name": "Alice Example", "role": "owner"}
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

_cache: dict[str, dict[str, Any] | None] = {}


def lookup_user(telegram_user_id: str) -> dict[str, Any] | None:
    """Resolve a Telegram user to their tenant and role.

    Returns:
        Dict with tenant_id, display_name, role — or None if unregistered.
    """
    if telegram_user_id in _cache:
        return _cache[telegram_user_id]

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT tenant_id, display_name, role, id::TEXT as user_id "
                "FROM tenant_users "
                "WHERE telegram_user_id = %s AND is_active = TRUE",
                (telegram_user_id,),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("Failed to look up tenant_user for %s", telegram_user_id)
        return None

    if row:
        result: dict[str, Any] = {
            "tenant_id": row[0],
            "display_name": row[1],
            "role": row[2],
            "user_id": row[3],
        }
        _cache[telegram_user_id] = result
        return result

    _cache[telegram_user_id] = None
    return None


def clear_cache() -> None:
    """Clear the user lookup cache (e.g., after registering a new user)."""
    _cache.clear()
