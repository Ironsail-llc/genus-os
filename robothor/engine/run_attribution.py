"""Run attribution — who and what a freshly started run is recorded against.

Extracted from ``AgentRunner.execute`` (same pattern as run_finalizer.py: a
cohesive cluster moved out of the god-object without changing behaviour).
Runs once per run, after the session exists and before setup starts.

Covers, in order (the order matters — each step reads the one before):

  1. sub-agent inheritance from the ``SpawnContext`` — parent run link,
     nesting depth, user identity, Contact 360 person and identity;
  2. capability tier, from the final (post-spawn) user_role;
  3. Contact 360 person linkage (run_person_link.py);
  4. the effective identity stashed on the session, so ``_run_loop`` can
     carry it onto a fresh SpawnContext for any children this run spawns;
  5. hierarchical tenant access (run_identity.py).

CONTRACT: plain functions over the session and their arguments; nothing from
AgentRunner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.engine.capability_tier import stamp_capability_tier
from robothor.engine.run_identity import attach_tenant_access
from robothor.engine.run_person_link import link_run_person

if TYPE_CHECKING:
    from robothor.engine.models import SpawnContext, TriggerType
    from robothor.engine.session import AgentSession


def inherit_from_spawn(
    session: AgentSession, spawn_context: SpawnContext | None, identity: Any
) -> Any:
    """Link a sub-agent to its parent run and inherit the parent's identity.

    Returns the effective identity: the run's own when it has one, otherwise
    the parent's.
    """
    # Sub-agent: link to parent run + inherit user identity. An empty
    # parent_run_id means the parent's own row was never recorded
    # (tracking_disabled) — insert NULL rather than a dangling FK that
    # would sink this child's entire row.
    if spawn_context:
        session.run.parent_run_id = spawn_context.parent_run_id or None
        session.run.nesting_depth = spawn_context.nesting_depth
        if not session.run.user_id and spawn_context.user_id:
            session.run.user_id = spawn_context.user_id
            session.run.user_role = spawn_context.user_role
        # Contact 360 linkage — inherit parent's person.
        if spawn_context.person_id:
            session.run.person_id = spawn_context.person_id
        # Identity — inherit parent's for person_id/user_id attribution
        # only. A child's own trigger_type is SUB_AGENT, which never
        # qualifies for the CURRENT USER prompt block (see the warmup /
        # mini-preamble gating in execute), so this never leaks a
        # prompt section into a worker's context — attribution only.
        if identity is None and spawn_context.identity:
            identity = spawn_context.identity
    return identity


async def attribute_run(
    session: AgentSession,
    spawn_context: SpawnContext | None,
    identity: Any,
    trigger_type: TriggerType,
    trigger_detail: str | None,
    tenant_id: str,
) -> tuple[Any, Any]:
    """Run steps 1-5 of the module docstring; returns ``(identity, tier)``.

    ``tier`` is the stamped capability tier, None while the feature is off.
    """
    identity = inherit_from_spawn(session, spawn_context, identity)

    # Capability tier from the final (post-spawn) user_role; None while off.
    tier = stamp_capability_tier(session.run, trigger_type, identity)

    # Contact 360 linkage (robothor/engine/run_person_link.py).
    await link_run_person(session.run, identity, trigger_type, trigger_detail)

    # Stash the effective identity on the session so _run_loop can carry
    # it onto a fresh SpawnContext for any children this run spawns.
    session.identity = identity

    await attach_tenant_access(session.run, tenant_id)
    return identity, tier
