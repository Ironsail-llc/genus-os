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
    from robothor.engine.run_context import in_benchmark_run

    simple_profile = (
        request.context.parent_id is None
        and options.get("trigger_type") in {"webchat", "telegram"}
        and getattr(options.get("agent_config"), "difficulty_class", "") == "simple"
    )
    if in_benchmark_run() or not (simple_profile or _confirmed_operation(request)):
        return request
    deadline = datetime.now(UTC) + timedelta(seconds=SIMPLE_ACTION_SECONDS)
    if request.context.deadline is not None:
        deadline = min(deadline, request.context.deadline)
    # This policy only restricts time. Native tool admission still checks
    # identity, permissions, arguments and any required confirmation.
    return replace(request, context=replace(request.context, deadline=deadline))


def _confirmed_operation(request):
    from robothor.engine.calendar_operations import confirmation_id
    from robothor.settings import get_settings

    history = request.options.get("conversation_history") or []
    if not isinstance(request.message, str) or not isinstance(history, list):
        return False
    if not history or not isinstance(history[-1], dict):
        return False
    return bool(
        confirmation_id(request.message, history)
        and get_settings().engine.calendar_operations_enabled
    )
