"""Native setup shared by runtime admissions; preserves identity and plan-only resumption."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime


async def restored_context(
    run_id, agent_id, tenant, trigger, readonly, execution, identity, user_id, role
):
    from robothor.engine.checkpoint import CheckpointManager
    from robothor.engine.models import TriggerType
    from robothor.engine.task_context import read_context

    if run_id:
        saved = await asyncio.to_thread(CheckpointManager.load_latest, run_id, tenant_id=tenant)
        context = read_context((saved or {}).get("messages") or [])
        if context and context.get("mode") == "plan":
            readonly, execution = True, False
        if (
            context
            and trigger == TriggerType.EVENT
            and identity is None
            and context.get("agent_id") == agent_id
        ):
            from robothor.identity import resolve_identity

            original = context.get("identity") or {}
            if original.get("tenant_id") == tenant:
                restored = await asyncio.to_thread(
                    resolve_identity,
                    original.get("channel", ""),
                    original.get("identifier", ""),
                    tenant_id=tenant,
                )
                if restored and restored.verified:
                    identity = restored
                    user_id = identity.user_account_id or identity.tenant_user_id or ""
                    role = identity.role
    return readonly, execution, identity, user_id, role


def principal(config, agent_id, trigger, user_id, role, spawn, system_triggers):
    if spawn and not user_id and spawn.user_id:
        user_id, role = spawn.user_id, spawn.user_role
    if trigger in system_triggers:
        return user_id or f"service:{agent_id}", role or config.service_role or "service"
    if not user_id or not role:
        from robothor.auth.runtime import auth_required

        if auth_required(bind_host=os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")):
            return None
        return user_id or "loopback-development-operator", role or "owner"
    return user_id, role


def bounded_timeout(timeout, session):
    from robothor.engine.runtime.current import active_context
    from robothor.engine.runtime.deadlines import owns_deadline

    context = active_context.get()
    if context and context.deadline and not owns_deadline(context):
        remaining = max(0, (context.deadline - datetime.now(UTC)).total_seconds())
        timeout = min(timeout, remaining) if timeout else remaining
    if getattr(session, "routine_operation_id", None):
        timeout = min(timeout, 60) if timeout else 60
    return timeout


async def attach_session(session):
    from robothor.engine.runtime.activity import register
    from robothor.goals.runtime import attach_run

    await asyncio.to_thread(attach_run, session.run)
    register(session)


def initialize_budget(run, config, spawn):
    from robothor.goals.runtime import initialize_token_budget

    initialize_token_budget(run, config, spawn)
