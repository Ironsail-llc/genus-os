"""Refuse to admit a remote principal when tenant isolation is decoration.

The responder wraps every inbound op in `tenant_scope`, so a tool that forgets
its WHERE clause still cannot cross tenants. That holds only while row-level
security is actually enforcing, and there are two quiet ways it is not: the
flag unset, or the connection being a SUPERUSER — which bypasses RLS
unconditionally and is the ordinary shape of a single-box install. That second
one is not hypothetical here: two services ran that way for their entire
existence while the instance reported RLS enabled.

Activating a federation link in that state would ship the third layer as a
comment. The check runs at activation, not per-op, because it is a property of
the deployment rather than of the request.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Escape hatch for an operator federating two of their own instances on one
#: box. Deliberately verbose, and deliberately not a default.
OVERRIDE_ENV = "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS"

_FIX = (
    "Set ROBOTHOR_RLS_ENABLED=1 and run the engine as a non-superuser "
    "(ROBOTHOR_DB_USER=robothor_app, migration 082) — see "
    "docs/runbooks/TENANT_RLS.md. To federate anyway and accept that inbound "
    f"ops are not tenant-isolated, set {OVERRIDE_ENV}=1."
)


class RLSInertError(RuntimeError):
    """Row-level security is not enforcing, so tenancy is not a boundary."""


def _rls_flag_on() -> bool:
    return os.environ.get("ROBOTHOR_RLS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _connection_is_superuser() -> bool:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
        return bool(row and row[0])


def require_enforcing_rls() -> None:
    """Raise unless tenant isolation is genuinely in force.

    Every failure path raises. An unanswerable probe is a refusal too: "we
    could not tell" must not admit a remote principal, because the whole
    lesson of the inert-control defects on this box is that silence was read
    as success.
    """
    if os.environ.get(OVERRIDE_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning(
            "Federation: activating with %s set — inbound ops from this peer are "
            "NOT tenant-isolated.",
            OVERRIDE_ENV,
        )
        return

    if not _rls_flag_on():
        raise RLSInertError(
            "Refusing to activate a federation link: ROBOTHOR_RLS_ENABLED is not "
            "set, so the tenant scope around every inbound op enforces nothing. " + _FIX
        )

    try:
        is_superuser = _connection_is_superuser()
    except Exception as exc:
        raise RLSInertError(
            "Refusing to activate a federation link: could not determine whether "
            f"row-level security is enforcing ({exc}). Tenant isolation is "
            "unknown, and unknown is not a licence. " + _FIX
        ) from exc

    if is_superuser:
        raise RLSInertError(
            "Refusing to activate a federation link: ROBOTHOR_RLS_ENABLED is set "
            "but this connection is a SUPERUSER, which bypasses row-level "
            "security unconditionally. There is no tenant isolation on inbound "
            "federation ops. " + _FIX
        )
