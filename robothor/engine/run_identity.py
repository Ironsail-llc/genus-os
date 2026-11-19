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

import asyncio
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


async def attach_tenant_access(run: Any, tenant_id: str, identity: Any = None) -> None:
    """Resolve the tenants this run may reach; any failure leaves its own tenant only.

    Reach beyond the run's own tenant is what lets a parent tenant's agent read
    and act in its children (``tools/cross_tenant.py``), so it is granted
    narrowly:

    * a sub-agent or benchmark run: its own tenant, always. A sub-agent
      inherits its parent's user for attribution, which is not authority;
    * an attended run -- a verified human, on a trigger a human starts: what
      that human reaches from the run's tenant (owner and admin: the
      descendants), judged by the WEAKER of the identity's role and the run's
      ``user_role``, and nothing beyond the run's own tenant when the identity
      was resolved in another tenant. An owner switched into a child resolves
      in that child (``identity.resolvers._resolve_webchat``), so reaches the
      child and the child's own descendants, never the parent or a sibling;
    * an unattended run -- a trigger in ``UNATTENDED_GRANT_TRIGGERS`` (cron and
      heartbeat, hook, event, workflow, channel event): its own tenant, plus
      the descendants only when BOTH the tenant
      (``crm_tenants.child_data_access``) and the agent's manifest
      (``v2.child_tenant_access``) grant it. Its ``service_role`` is not a
      person and grants nothing here;
    * anything else -- a person-started trigger with no verified identity, a
      federated request: its own tenant only.

    The lookups are DB and file reads, so they run in a worker thread.
    """
    own = (tenant_id,)
    try:
        run.accessible_tenant_ids = await asyncio.to_thread(
            _accessible_tenants, run, tenant_id, identity
        )
    except Exception:
        logger.warning("tenant access lookup failed; restricting run to its own tenant")
        run.accessible_tenant_ids = own


#: ``trigger_detail`` prefix of the engine-started repair continuation
#: (``repair_recovery.py``): a MANUAL run carrying the owner's identity for
#: attribution, with nobody present.
REPAIR_RESUME_DETAIL = "repair-resume:"


def confined_to_own_tenant(run: Any) -> bool:
    """A run that never reaches another tenant, whatever it inherited.

    A sub-agent, a benchmark run, and the repair continuation. The sub-agent
    trigger and the nesting depth count as well as the parent id, because a
    child of a ``tracking_disabled`` parent records no parent id. Shared with
    the per-call check in ``tools/cross_tenant.py``. Confinement only removes
    reach; it changes nothing a run may do in its own tenant.
    """
    from robothor.engine.models import TriggerType

    return bool(
        getattr(run, "is_benchmark", False)
        or getattr(run, "parent_run_id", None)
        or int(getattr(run, "nesting_depth", 0) or 0) > 0
        or getattr(run, "trigger_type", None) == TriggerType.SUB_AGENT
        or str(getattr(run, "trigger_detail", None) or "").startswith(REPAIR_RESUME_DETAIL)
    )


def _grant_triggers() -> frozenset[Any]:
    from robothor.engine.models import TriggerType

    return frozenset(
        {
            TriggerType.CRON,  # heartbeats included
            TriggerType.HOOK,
            TriggerType.EVENT,
            TriggerType.WORKFLOW,
            TriggerType.CHANNEL_EVENT,
        }
    )


#: The triggers the unattended grant (both flags) applies to: scheduled and
#: system work with no person behind it. A subset of the runner's
#: ``_SYSTEM_TRIGGER_TYPES`` without the sub-agent (always confined) and
#: federation (a peer instance's request is not this tenant's own work). A
#: trigger a person starts -- webchat, telegram, slack, a plugin channel,
#: manual, ide, webhook -- is not here: without a verified human it reaches
#: only its own tenant, whatever the flags say.
UNATTENDED_GRANT_TRIGGERS = _grant_triggers()


def _accessible_tenants(run: Any, tenant_id: str, identity: Any) -> tuple[str, ...]:
    from robothor.engine.permissions import resolve_accessible_tenants

    own = (tenant_id,)
    if confined_to_own_tenant(run):
        return own
    from robothor.engine.runner import _SYSTEM_TRIGGER_TYPES

    attended = (
        identity is not None
        and bool(getattr(identity, "verified", False))
        and getattr(run, "trigger_type", None) not in _SYSTEM_TRIGGER_TYPES
    )
    if attended:
        # The verified identity is the authority; the run's role only narrows
        # it. An identity resolved in another tenant vouches for nothing here.
        if getattr(identity, "tenant_id", None) != tenant_id:
            return own
        role = _weaker_role(getattr(identity, "role", None), getattr(run, "user_role", None))
        return tuple(resolve_accessible_tenants(tenant_id, role))
    if getattr(run, "trigger_type", None) not in UNATTENDED_GRANT_TRIGGERS:
        # A person started this run but nobody vouched for them: no grant.
        return own
    from robothor.crm.dal import tenant_child_data_access

    if tenant_child_data_access(tenant_id) and _agent_opts_in(getattr(run, "agent_id", "")):
        # The role here only unlocks the descendant walk; the grant is above.
        return tuple(resolve_accessible_tenants(tenant_id, "admin"))
    return own


#: The roles that reach descendant tenants, strongest first.
_REACHING_ROLES = ("owner", "admin")


def _weaker_role(first: Any, second: Any) -> str | None:
    """The less privileged of two roles, for reach: a role outside
    ``_REACHING_ROLES`` (member, viewer, none) is weaker than both."""
    if first not in _REACHING_ROLES or second not in _REACHING_ROLES:
        return None
    return str(max(first, second, key=_REACHING_ROLES.index))


def _agent_opts_in(agent_id: str) -> bool:
    """The agent manifest's ``v2.child_tenant_access``. Unreadable: not opted in."""
    if not agent_id:
        return False
    try:
        from robothor.engine.config import EngineConfig, load_agent_config

        cfg = load_agent_config(agent_id, EngineConfig.from_env().manifest_dir)
    except Exception:  # noqa: BLE001 - absence of evidence is not an opt-in
        logger.warning("child_tenant_access: manifest for %s unreadable", _sanitize(agent_id))
        return False
    return bool(cfg is not None and cfg.child_tenant_access)


async def follow_up_preamble(
    agent_id: str,
    identity: Any,
    tenant_id: str,
    accessible_tenant_ids: tuple[str, ...],
) -> str:
    """The engine turn for an interactive follow-up that skipped warmup.

    The first turn got the CURRENT USER and ORGANISATION blocks from warmup,
    but neither is re-sent as part of the conversation history, so every turn
    after it needs its own reminder of who is talking and where. Nothing else:
    memory blocks, entity context and the rest are already in the transcript.

    Both reads are blocking DB work, so both run off the event loop: the
    identity enrichment (held for the chat session) on the executor, the
    organisation lookup in a worker thread. A failure costs its block, never
    the turn.
    """
    parts: list[str] = []
    if identity is not None:
        try:
            from robothor.engine.warmup import enrichment_call_for_turn

            enrich_fn, enrich_args = enrichment_call_for_turn(agent_id, identity)
            loop = asyncio.get_running_loop()
            enriched = await loop.run_in_executor(None, enrich_fn, *enrich_args)
        except Exception as e:
            logger.debug(
                "Per-turn identity enrichment failed for %s: %s", _sanitize(agent_id), _sanitize(e)
            )
            enriched = None
        try:
            parts.append(identity.prompt_block(enriched))
        except Exception as e:
            logger.debug(
                "Per-turn identity block failed for %s: %s", _sanitize(agent_id), _sanitize(e)
            )
    try:
        from robothor.engine import org_context

        block = await asyncio.to_thread(
            org_context.organisation_block, tenant_id, accessible_tenant_ids
        )
    except Exception as e:
        logger.warning("Per-turn organisation block failed: %s", _sanitize(e))
        block = ""
    if block:
        parts.append(block)
    return "\n\n".join(parts)


async def resolve_run_identity_off_loop(identity: Any, **kwargs: Any) -> Any:
    """:func:`resolve_run_identity` in a worker thread (a webchat miss is a DB query)."""
    return await asyncio.to_thread(resolve_run_identity, identity, **kwargs)


def seat_session(
    session: Any,
    agent_config: Any,
    live_inbox: Any,
    typed_message: str | None,
    actor: tuple[str, str],
    resume_from_run_id: str | None,
) -> None:
    """Seat the run's caller, inbox and per-agent settings on a fresh session.

    ``typed_message`` is the raw text the person typed, when a channel
    assembled the run's message around it (a reply quote, attachment text);
    credential intake reads only this. ``actor`` is ``principal()``'s
    ``(user_id, user_role)``.
    """
    session.live_inbox = live_inbox  # chat sent mid-run; see live_inbox.py
    session.typed_message = typed_message or ""
    session.response_format = agent_config.response_format
    session.provider_order = agent_config.provider_order
    session.run.user_id, session.run.user_role = actor
    session.run.resume_from_run_id = resume_from_run_id
