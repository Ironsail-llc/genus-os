"""Restore saved request limits without renewing a checkpoint's authority."""

from dataclasses import replace
from datetime import datetime


def restore(context, checkpoint):
    saved = checkpoint.get("runtime_context") or {}
    if not isinstance(saved, dict):
        raise ValueError("invalid saved runtime context")
    # A trusted goal controller can grant a new attempt its own deadline.
    # CurrentRuntime validates the active goal lease before reaching this point.
    if (
        context.goal_id
        and saved.get("goal_id") == context.goal_id
        and saved.get("attempt_id") != context.attempt_id
    ):
        return context
    value = saved.get("deadline")
    if value is None:
        return context  # Legacy checkpoints have no saved runtime bound.
    try:
        deadline = datetime.fromisoformat(value)
        if deadline.tzinfo is None:
            raise ValueError("missing timezone")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid saved runtime deadline") from exc
    if context.deadline is not None:
        deadline = min(deadline, context.deadline)
    return replace(context, deadline=deadline)
