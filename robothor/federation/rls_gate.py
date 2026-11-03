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
from typing import NamedTuple

logger = logging.getLogger(__name__)

#: Escape hatch for an operator federating two of their own instances on one
#: box. Deliberately verbose, and deliberately not a default.
OVERRIDE_ENV = "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS"

_FEDERATION_CONTEXT = "activate a federation link"


class _Wording(NamedTuple):
    """The context-specific phrases in the gate's messages."""

    scope: str  # what the tenant scope wraps
    exposure: str  # where isolation is missing, appended to "no tenant isolation"
    anyway: str  # how the override sentence begins
    overridden: str  # the warning logged when the override admits the caller


def _wording(context: str) -> _Wording:
    if context == _FEDERATION_CONTEXT:
        return _Wording(
            scope="the tenant scope around every inbound op",
            exposure=" on inbound federation ops",
            anyway="To federate anyway and accept that inbound ops are not tenant-isolated",
            overridden="Federation: activating with %s set — inbound ops from this peer "
            "are NOT tenant-isolated.",
        )
    return _Wording(
        scope="the tenant scope",
        exposure="",
        anyway=f"To {context} anyway and accept that tenancy is not enforced",
        overridden=f"Allowed to {context} with %s set — row-level security is not "
        "verified, so tenancy may not be enforced.",
    )


def fix_text(override_env: str, context: str) -> str:
    """How to make the gate pass, naming THIS caller's override."""
    return (
        "Set ROBOTHOR_RLS_ENABLED=1 and run the engine as a non-superuser "
        "(ROBOTHOR_DB_USER=robothor_app, migration 082) — see "
        f"docs/runbooks/TENANT_RLS.md. {_wording(context).anyway}, set {override_env}=1."
    )


#: Kept for callers that imported the federation text by name.
_FIX = fix_text(OVERRIDE_ENV, _FEDERATION_CONTEXT)


class RLSInertError(RuntimeError):
    """Row-level security is not enforcing, so tenancy is not a boundary."""


def _rls_flag_on() -> bool:
    return os.environ.get("ROBOTHOR_RLS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


#: The superuser verdict, once it has been answered. The role a process
#: connects as does not change under it, and the tenant-switch endpoint calls
#: the gate on every request — the same reason ``_apply_tenant_scope`` probes
#: once per process. A probe that FAILED is not cached: "could not tell" must
#: be asked again, never remembered as an answer.
_superuser_verdict: bool | None = None


def reset_superuser_cache() -> None:
    """Forget the cached superuser verdict. For tests."""
    global _superuser_verdict
    _superuser_verdict = None


def _probe_superuser() -> bool:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
        return bool(row and row[0])


def _connection_is_superuser() -> bool:
    global _superuser_verdict
    if _superuser_verdict is None:
        _superuser_verdict = _probe_superuser()
    return _superuser_verdict


def require_enforcing_rls(
    *,
    override_env: str = OVERRIDE_ENV,
    context: str = _FEDERATION_CONTEXT,
    override: bool | None = None,
) -> None:
    """Raise unless tenant isolation is genuinely in force.

    Every failure path raises. An unanswerable probe is a refusal too: "we
    could not tell" must not admit a remote principal, because the whole
    lesson of the inert-control defects on this box is that silence was read
    as success.

    Shared by federation activation (the defaults, unchanged) and the bridge's
    ``POST /api/auth/switch-tenant``, which passes its own ``override_env`` and
    ``context`` so the messages name the switch and its own escape hatch.

    Args:
        override_env: the variable that deliberately accepts inert RLS, named
            in every message.
        context: what is being refused, as a verb phrase ("switch tenants").
        override: the override already resolved by the caller (the bridge
            reads it through settings, so its alias is honoured). ``None``
            reads ``override_env`` from the environment.
    """
    overridden = _truthy(os.environ.get(override_env)) if override is None else override
    words = _wording(context)
    fix = fix_text(override_env, context)
    if overridden:
        logger.warning(words.overridden, override_env)
        return

    if not _rls_flag_on():
        raise RLSInertError(
            f"Refusing to {context}: ROBOTHOR_RLS_ENABLED is not set, so "
            f"{words.scope} enforces nothing. " + fix
        )

    try:
        is_superuser = _connection_is_superuser()
    except Exception as exc:
        raise RLSInertError(
            f"Refusing to {context}: could not determine whether "
            f"row-level security is enforcing ({exc}). Tenant isolation is "
            "unknown, and unknown is not a licence. " + fix
        ) from exc

    if is_superuser:
        raise RLSInertError(
            f"Refusing to {context}: ROBOTHOR_RLS_ENABLED is set "
            "but this connection is a SUPERUSER, which bypasses row-level "
            f"security unconditionally. There is no tenant isolation{words.exposure}. " + fix
        )
