"""Whether a person may act in a company other than their own (switched in).

The rule ``POST /api/auth/switch-tenant`` applies when it mints an acting
token, for a service that is told who is acting rather than handed their
token: the account is active and lives in its home company, it is an owner or
admin there, and the company it acts in is among that home's descendants.
Read here, bound to the home company (``tenant_scope``: under row-level
security the account is visible only there). Unlike a session's standing
(``session_standing``), a failure to read refuses: this admits someone into a
company that is not theirs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Who may act in a descendant company.
SWITCH_ROLES = frozenset({"owner", "admin"})


def acting_role(user_id: str, home: str, here: str) -> str | None:
    """The role ``user_id`` acts with in ``here`` from ``home``, or None.

    Blocking (a SELECT, and the hierarchy's). Never raises."""
    from robothor.auth import accounts
    from robothor.db.connection import tenant_scope
    from robothor.engine.permissions import resolve_accessible_tenants

    try:
        with tenant_scope(home):
            account = accounts.get_account_by_id(user_id)
            if (
                not account
                or str(account.get("tenant_id") or "") != home
                or account.get("status") != "active"
                or account.get("role") not in SWITCH_ROLES
            ):
                return None
            role = str(account["role"])
            if here not in resolve_accessible_tenants(home, role):
                return None
    except Exception:  # noqa: BLE001 - cannot check, so refuse
        logger.warning("could not check a switched-in requester; refused", exc_info=True)
        return None
    return role
