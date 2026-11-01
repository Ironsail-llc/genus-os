"""The capability tier: what the HUMAN's role lets their session be offered.

RBAC answers "may this role call this tool?" at dispatch, but only for tools
the run was offered in the first place. An agent with no ``tools_allowed`` is
offered everything minus the opt-in families (``OPT_IN_TOOLS``) unless its
manifest names them — so the owner, who holds a ``*`` allow, asks for a coding
job and is told the tool is "not available to this agent". The role was known
(``AgentRun.user_role``); nothing turned it into a capability.

The tier is that value. Rules:

* It only ever WIDENS. ``standard`` and ``restricted`` change nothing; every
  refusal (RBAC, plan mode, approval, secret paths, destructive commands,
  injection) still applies to every tier.
* A tier above ``standard`` needs an interactive run AND a verified identity.
  Cron, hook, workflow and sub-agent runs are governed by the agent's
  ``service_role``; a fabricated identity (an unregistered Telegram sender in
  the operator's chat is handed role ``owner`` with ``verified=False``) proves
  nothing about who is typing.
* ``ROBOTHOR_ROLE_CAPABILITIES_MODE``: ``observe`` stamps the tier on the run
  and records what it would unlock; only ``enforce`` makes it effective.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)

Tier = Literal["unrestricted", "elevated", "standard", "restricted"]

ROLE_TIERS: dict[str, Tier] = {
    "owner": "unrestricted",
    "admin": "elevated",
    "member": "standard",
    "user": "standard",
    "service": "standard",
    "viewer": "restricted",
    "auditor": "restricted",
    "guest": "restricted",
}

#: Tiers that add the opt-in tool families to an agent with no tools_allowed.
WIDENING_TIERS: frozenset[str] = frozenset({"unrestricted", "elevated"})

GUARDRAIL_NAME = "role_capability"


@dataclass(frozen=True)
class TierDecision:
    tier: Tier
    role: str
    reason: str
    mode: str

    @property
    def effective(self) -> str:
        """The tier gates may act on: the tier under enforce, else ``""``."""
        return self.tier if self.mode == "enforce" else ""

    @property
    def widens(self) -> bool:
        return self.tier in WIDENING_TIERS


def resolve_tier(
    role: str, *, interactive: bool, identity_verified: bool, mode: str
) -> TierDecision:
    """Map a human's role to a tier, refusing to widen without proof of who it is."""
    tier: Tier = ROLE_TIERS.get(role, "standard")
    if tier in WIDENING_TIERS and not interactive:
        return TierDecision("standard", role, "not an interactive run", mode)
    if tier in WIDENING_TIERS and not identity_verified:
        return TierDecision("standard", role, "identity not verified", mode)
    return TierDecision(tier, role, f"role {role or 'unknown'}", mode)


def stamp_capability_tier(run: Any, trigger_type: Any, identity: Any) -> TierDecision | None:
    """Resolve this run's tier and record it on the run. None when the flag is off.

    Called once per run, after the human's role and identity are resolved. A
    trigger in the runner's ``_SYSTEM_TRIGGER_TYPES`` has no interactive human.
    """
    from robothor.engine.feature_flags import role_capabilities_mode

    mode = role_capabilities_mode()
    if mode == "off":
        return None
    from robothor.engine.runner import _SYSTEM_TRIGGER_TYPES

    decision = resolve_tier(
        run.user_role,
        interactive=trigger_type not in _SYSTEM_TRIGGER_TYPES,
        identity_verified=bool(identity is not None and getattr(identity, "verified", False)),
        mode=mode,
    )
    run.capability_tier = decision.tier
    run.capability_tier_mode = decision.mode
    run.capability_tier_reason = decision.reason
    return decision


#: The effective tier of the run whose tools are executing. Installed and
#: withdrawn with the run's toolset (toolset_prep.publish_toolset), so a
#: sub-agent run inline in its parent's task restores the parent's on exit.
_effective_tier_var: ContextVar[str] = ContextVar("capability_tier", default="")


def set_effective_tier(tier: str) -> Token[str]:
    return _effective_tier_var.set(tier)


def clear_effective_tier(token: Token[str]) -> None:
    _effective_tier_var.reset(token)


def current_effective_tier() -> str:
    """What ``ToolContext.capability_tier`` carries: the tier under enforce, else ``""``."""
    return _effective_tier_var.get()


def record_tier_event(run_id: str, decision: TierDecision, unlocked: tuple[str, ...]) -> None:
    """One ``agent_guardrail_events`` row for a widened run. Blocking: call off the loop."""
    tools = ", ".join(unlocked) if unlocked else "none"
    verb = "unlocked" if decision.mode == "enforce" else "would unlock"
    reason = f"role {decision.role} -> tier {decision.tier}; {verb} opt-in tools: {tools}"
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id=run_id,
            guardrail_name=GUARDRAIL_NAME,
            action="allowed" if decision.mode == "enforce" else "observed",
            reason=reason,
            mode=decision.mode,
            step_number=0,
        )
    except Exception as exc:  # noqa: BLE001 - losing the audit row is logged, never fatal
        logger.error("role_capability event could not be recorded: %s", _sanitize(exc))
