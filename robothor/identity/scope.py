"""Row-level "own data + shared" data scoping (Task 5, Unified Identity Context).

RBAC (``robothor.engine.permissions``) answers "may this caller invoke this
tool at all?". ``DataScope`` answers a narrower question underneath that:
once a caller is allowed to run a data-read tool, which ROWS may it see?

The operator-approved model: non-privileged identities (role not in
``{owner, admin, service}``) may only draw on rows linked to their own
``person_id``, or rows with ``person_id IS NULL`` (org-general — nobody's
personal data). Owner/admin/service identities, and any caller with no
resolved identity at all (system/cron/heartbeat runs — see
``robothor.engine.runner`` and ``_SYSTEM_TRIGGER_TYPES``), see everything
in-tenant unchanged.

This module is deliberately independent of the ``ROBOTHOR_DATA_SCOPING``
flag ladder (``robothor.engine.feature_flags.data_scoping_mode``): scoping
math here is pure and unconditional; the *flag* decides whether a tool
handler is allowed to hand the resulting ``DataScope`` to a DAL query
(``enforce``), merely observe what it would have dropped (``observe``), or
never compute it at all in practice (``off``). See ``scope_for_query`` and
``observe_scope`` for that wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from robothor.identity.context import IdentityContext

logger = logging.getLogger(__name__)

# Roles that see all in-tenant rows regardless of person_id linkage.
PRIVILEGED_ROLES = frozenset({"owner", "admin", "service"})


@dataclass(frozen=True)
class DataScope:
    """The row-visibility scope for one tool call.

    ``restricted=False`` means "no extra filtering" — every existing caller
    (identity=None, or owner/admin/service) sees the same rows it always
    did. ``restricted=True`` means the "own data + shared" rule applies:
    rows with ``person_id == self.person_id`` or ``person_id IS NULL`` are
    visible; everything else is not.
    """

    tenant_id: str
    person_id: str | None
    restricted: bool


def scope_for(identity: IdentityContext | None) -> DataScope:
    """Compute the DataScope for a resolved identity.

    ``identity is None`` (system/cron/heartbeat runs that never resolve an
    interactive identity — the pre-existing, unaffected path) always yields
    an unrestricted scope. Otherwise, restricted iff the identity's role is
    not one of the privileged roles — a missing/empty role on a *present*
    identity counts as restricted (fail toward the narrower scope, not the
    all-access one).
    """
    if identity is None:
        return DataScope(tenant_id="", person_id=None, restricted=False)
    restricted = (identity.role or "") not in PRIVILEGED_ROLES
    return DataScope(
        tenant_id=identity.tenant_id, person_id=identity.person_id, restricted=restricted
    )


def scope_for_query(mode: str, identity: IdentityContext | None) -> DataScope | None:
    """The scope a DAL query should actually filter on, given the rollout mode.

    Returns a real (restricted) ``DataScope`` only when ``mode == "enforce"``
    AND the identity is actually restricted. Every other combination
    (``off``, ``observe``, or a privileged/absent identity) returns ``None``
    — DAL functions treat ``None`` as "unrestricted, don't touch the query",
    so ``off`` mode and non-restricted callers are byte-identical to
    pre-Task-5 behavior.
    """
    if mode != "enforce":
        return None
    scope = scope_for(identity)
    return scope if scope.restricted else None


def observe_scope(mode: str, identity: IdentityContext | None) -> DataScope | None:
    """The scope to use for observe-mode dry-run counting only.

    Returns a real (restricted) ``DataScope`` only when ``mode == "observe"``
    AND the identity is actually restricted. ``enforce`` returns ``None``
    here because the query itself already filtered — there's nothing left to
    observe. ``off`` and privileged/absent identities also return ``None``.
    """
    if mode != "observe":
        return None
    scope = scope_for(identity)
    return scope if scope.restricted else None


def rows_dropped_by_scope(
    rows: Iterable[dict[str, Any]],
    scope: DataScope,
    *,
    person_key: str = "person_id",
) -> int:
    """Count how many of ``rows`` the "own data + shared" rule would drop.

    A row is dropped iff it carries a ``person_key`` value that is neither
    ``None`` (org-general) nor the scope's own ``person_id``. Used only for
    observe-mode logging — never to actually filter a result set.
    """
    if not scope.restricted:
        return 0
    return sum(1 for r in rows if r.get(person_key) not in (None, scope.person_id))


def rows_dropped_by_identity_scope(
    rows: Iterable[dict[str, Any]],
    scope: DataScope,
    *,
    id_key: str = "id",
) -> int:
    """Own-row-only variant for tables that ARE the person (``crm_people``).

    There is no org-general carve-out here — a person row belongs to exactly
    one person. A row is dropped unless ``row[id_key] == scope.person_id``.
    """
    if not scope.restricted:
        return 0
    return sum(1 for r in rows if r.get(id_key) != scope.person_id)


def log_would_drop(
    *,
    tool_name: str,
    user_id: str | None,
    scope: DataScope,
    dropped: int,
    table: str,
) -> None:
    """Structured observe-mode log line — silent when nothing would drop."""
    if dropped <= 0:
        return
    logger.info(
        "data_scoping: would_drop=%d tool=%s user=%s person=%s table=%s",
        dropped,
        tool_name,
        user_id or "",
        scope.person_id or "",
        table,
    )
