"""Prefer throughput only for the qualified, bounded ordinary-chat paths."""

from __future__ import annotations

from dataclasses import replace
from typing import Any


def prefer_throughput(session: Any) -> bool:
    from robothor.engine.runtime.classification_window import owned_deadline
    from robothor.engine.runtime.classified_deadline import admitted_request, eligible
    from robothor.engine.runtime.current import active_context

    request = admitted_request()
    context = active_context.get()
    if request is None or context is None:
        return False
    config = request.options.get("agent_config")
    if config is None or not eligible(request, config):
        return False
    # Deadline tightening is allowed; a different trusted execution identity
    # must not inherit this admission's routing preference.
    if replace(context, deadline=request.context.deadline) != request.context:
        return False
    if (
        getattr(session.run, "correlation_id", None) != context.request_id
        or getattr(session.run, "tenant_id", None) != context.tenant_id
        or getattr(config, "difficulty_class", "") not in {"", "simple"}
        or getattr(config, "planning_enabled", False)
        or getattr(config, "planning_model", "")
        or str(request.options.get("trigger_detail") or "").startswith("plan")
    ):
        return False
    if request.context.deadline is not None:
        # An explicit long deadline approaching expiry is not simple work.
        return getattr(config, "difficulty_class", "") == "simple"
    # Identified longer work releases its classification window. Do not infer
    # classification from remaining seconds or introduce another deadline owner.
    return owned_deadline() is not None
