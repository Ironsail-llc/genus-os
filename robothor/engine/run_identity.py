"""Who is this run's message addressed to?

Extracted from `execute`. Identity decides which person the agent believes it
is talking to — it drives the CURRENT USER prompt block and person attribution
— so its precedence is security-adjacent and worth stating in one place:

    explicit `identity=` kwarg
      > webchat resolution from the database
      > legacy Telegram `|sender:` parse

The legacy parse is back-compat until every caller passes `identity=`
explicitly. It is deliberately last: it trusts a string inside
`trigger_detail`, where the other two paths resolve against a record.

A service caller on webchat is skipped rather than resolved. A system-
triggered run has no human on the other end, and inventing one would attribute
autonomous work to whoever last used the channel.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


def resolve_run_identity(
    identity: Any,
    *,
    agent_id: str,
    trigger_type: Any,
    trigger_detail: str | None,
    user_id: str,
    user_role: str,
    tenant_id: str,
    is_service_caller: bool,
) -> Any:
    """The identity this run acts for, or None when there is no human."""
    if identity is not None:
        return identity

    from robothor.engine.models import TriggerType

    if trigger_type == TriggerType.WEBCHAT and not is_service_caller:
        from robothor.identity import resolve_identity

        return resolve_identity("webchat", user_id, tenant_id)

    if trigger_type == TriggerType.TELEGRAM and trigger_detail and "|sender:" in trigger_detail:
        from robothor.identity import IdentityContext

        sender = trigger_detail.split("|sender:", 1)[1]
        logger.debug(
            "execute: using legacy sender parse fallback for identity (agent=%s)",
            _sanitize(agent_id),
        )
        return IdentityContext(
            tenant_id=tenant_id,
            channel="telegram",
            identifier=user_id,
            # Verified means the caller arrived with BOTH an id and a role.
            # A display name parsed out of trigger_detail proves neither.
            verified=bool(user_id and user_role),
            display_name=sender,
            role=user_role or "",
        )

    return None


def _is_service_caller(user_role: str, user_id: str) -> bool:
    """Whether this run's effective caller is a service/automated actor.

    A WEBCHAT run can still arrive from a service-typ auth context (an
    engine/bridge credential acting on an agent's behalf, not a human — see
    ``AuthContext.is_service`` at the chat layer). ``chat.py`` already passes
    ``identity=None`` for those, but the runner can't tell "deliberately
    None" from "not provided", so the fallback below must re-derive
    service-ness itself from the same conventions used elsewhere in this
    module: the manifest's default ``service_role`` value of ``"service"``
    (``AgentConfig.service_role``, ``issue_service_token``'s default role)
    and the ``f"service:{agent_id}"`` / ``f"service:workflow:{id}"`` user_id
    marker convention (``_SYSTEM_TRIGGER_TYPES`` branch above, workflow.py,
    scheduler.py). Without this gate, a service caller's non-UUID user_id
    reaches ``resolve_identity("webchat", ...)`` and triggers a DB error on
    every single call until the negative cache absorbs it (60s TTL).
    """
    return (
        user_role == "service" or user_role.startswith("service:") or user_id.startswith("service:")
    )


def attach_tenant_access(run: Any, tenant_id: str) -> None:
    """Resolve hierarchical access, restricting failures to the run's own tenant."""
    from robothor.engine.permissions import resolve_accessible_tenants

    try:
        run.accessible_tenant_ids = resolve_accessible_tenants(tenant_id, run.user_role)
    except Exception:
        run.accessible_tenant_ids = (tenant_id,)
