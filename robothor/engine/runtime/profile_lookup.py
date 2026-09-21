"""Bound pre-execution profile I/O without blocking the chat event loop."""

import asyncio
from dataclasses import replace
from datetime import timedelta

LOOKUP_SECONDS = 60


async def lookup(request, directory, admitted_at):
    from robothor.engine.runner import load_agent_config_or_reason
    from robothor.engine.runtime.admission_audit import record_interrupted, record_timeout
    from robothor.engine.runtime.deadlines import (
        RuntimeDeadlineError,
        constrain_context,
        execute_before_deadline,
        remaining,
    )

    deadline = admitted_at + timedelta(seconds=LOOKUP_SECONDS)
    if request.context.deadline is not None:
        deadline = min(deadline, request.context.deadline)
    bounded = replace(
        request, context=constrain_context(replace(request.context, deadline=deadline))
    )
    try:
        return await execute_before_deadline(
            bounded.context,
            lambda: asyncio.to_thread(load_agent_config_or_reason, request.agent_id, directory),
        )
    except RuntimeDeadlineError:
        await record_timeout(bounded)
        raise
    except asyncio.CancelledError:
        if remaining(bounded.context) <= 0:
            await record_timeout(bounded)
        else:
            await record_interrupted(bounded)
        raise
