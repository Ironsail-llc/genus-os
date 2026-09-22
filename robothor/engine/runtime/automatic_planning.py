"""Limit optional interactive planning without changing the request's authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from robothor.engine.models import AgentConfig
    from robothor.engine.runtime.contracts import ExecutionContext

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from robothor.engine.runtime import classified_deadline
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import (
    RuntimeDeadlineError,
    execute_before_deadline,
    require_time,
)

logger = logging.getLogger(__name__)
AUTOMATIC_PLAN_SECONDS = 5


def context_for(config: AgentConfig) -> ExecutionContext | None:
    request = classified_deadline.admitted_request()
    context = active_context.get()
    if (
        request is None
        or context is None
        or context.goal_id
        or context.parent_id
        or not classified_deadline.eligible(request, config)
        or getattr(config, "planning_enabled", False)
        or getattr(config, "planning_model", "")
        or getattr(config, "difficulty_class", "")
        or str(request.options.get("trigger_detail", "")).startswith("plan")
    ):
        return None
    deadline = datetime.now(UTC) + timedelta(seconds=AUTOMATIC_PLAN_SECONDS)
    if context.deadline is not None:
        deadline = min(deadline, context.deadline)
    return replace(context, deadline=deadline)


async def run(config: AgentConfig, generate: Callable[[], Awaitable[Any]]) -> Any:
    context = context_for(config)
    if context is None:
        return await generate()
    try:
        # Reuse the deadline owner, including provider dispatch denial and
        # cancellation-resistant return checks. No detached planning task.
        return await execute_before_deadline(context, generate)
    except RuntimeDeadlineError:
        # The temporary owner has unwound. An expired outer request must still
        # fail; only expiration of this optional phase can continue execution.
        require_time()
        logger.info("Automatic planning budget expired; proceeding without an auxiliary plan")
        return None
