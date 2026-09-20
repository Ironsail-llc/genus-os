"""Record a native request that expired before entering the execution engine."""

import asyncio
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


async def record_timeout(request):
    from robothor.engine.models import AgentRun, RunStatus, TriggerType
    from robothor.engine.runtime.current import active_context
    from robothor.engine.tracking import create_run, update_run

    now = datetime.now(UTC)
    run = AgentRun(
        tenant_id=request.context.tenant_id,
        user_id=request.context.principal_id,
        user_role=request.options.get("user_role", ""),
        agent_id=request.agent_id,
        correlation_id=request.context.request_id,
        parent_run_id=request.context.parent_id,
        trigger_type=request.options.get("trigger_type", TriggerType.MANUAL),
        status=RunStatus.TIMEOUT,
        started_at=now,
        completed_at=now,
        error_message="This request expired before execution began. Earlier attempts retain their own recorded outcomes.",
    )

    def persist():
        create_run(run)
        update_run(
            run.id,
            status=run.status.value,
            completed_at=now,
            error_message=run.error_message,
        )

    token = active_context.set(request.context)
    try:
        # A read/setup timeout must not turn into an unbounded audit write.
        # A dispatched database write may finish after this wait expires.
        await asyncio.wait_for(asyncio.to_thread(persist), timeout=1)
    except Exception as exc:
        logger.warning("Admission timeout audit unavailable (%s)", type(exc).__name__)
    finally:
        active_context.reset(token)
