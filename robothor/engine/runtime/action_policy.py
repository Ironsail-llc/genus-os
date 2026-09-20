"""Assign a total deadline to supported simple actions without granting authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

SIMPLE_ACTION_SECONDS = 60


def apply_action_deadline(request):
    options = request.options
    if (
        request.resume_from
        or request.context.goal_id
        or options.get("spawn_context")
        or options.get("readonly_mode")
        or options.get("deep_plan")
        or getattr(options.get("agent_config"), "is_benchmark", False)
    ):
        return request
    history = options.get("conversation_history") or []
    if not isinstance(request.message, str) or not isinstance(history, list):
        return request
    if not history or not isinstance(history[-1], dict):
        return request
    from robothor.engine.calendar_operations import confirmation_id

    if not confirmation_id(request.message, history):
        return request
    from robothor.engine.run_context import in_benchmark_run
    from robothor.settings import get_settings

    if in_benchmark_run() or not get_settings().engine.calendar_operations_enabled:
        return request
    deadline = datetime.now(UTC) + timedelta(seconds=SIMPLE_ACTION_SECONDS)
    if request.context.deadline is not None:
        deadline = min(deadline, request.context.deadline)
    # The operation's arguments, scope and confirmation are still checked by
    # native tool admission. Recognition here only restricts available time.
    return replace(request, context=replace(request.context, deadline=deadline))
