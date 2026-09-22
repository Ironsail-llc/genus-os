"""Resolve interactive profiles once while retaining the native failure path."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from robothor.engine.runtime.contracts import RunRequest

#: (agent_id, manifest_dir, config, refusal_reason) from a completed lookup.
_Resolution = tuple[str, Path, Any, Any]

_resolution: ContextVar[_Resolution | None] = ContextVar("native_profile_resolution", default=None)


def needs_lookup(request: RunRequest) -> bool:
    from robothor.engine.run_context import in_benchmark_run

    options = request.options
    return not (
        options.get("agent_config") is not None
        or options.get("trigger_type") not in {"webchat", "telegram"}
        or request.resume_from
        or request.context.goal_id
        or request.context.parent_id
        or options.get("spawn_context")
        or options.get("readonly_mode")
        or options.get("deep_plan")
        or in_benchmark_run()
    )


async def prepare(
    runner: Any, request: RunRequest, admitted_at: datetime
) -> tuple[RunRequest, _Resolution | None]:
    from robothor.engine.runtime.action_policy import apply_action_deadline

    options = request.options
    if not needs_lookup(request):
        return apply_action_deadline(request, admitted_at=admitted_at), None
    from robothor.engine.runtime.profile_lookup import lookup

    directory = runner.config.manifest_dir
    config, reason = await lookup(request, directory, admitted_at)
    resolution = (request.agent_id, directory, config, reason)
    if config is not None:
        request = replace(request, options={**options, "agent_config": config})
    return apply_action_deadline(request, admitted_at=admitted_at), resolution


@contextmanager
def resolved_profile(resolution: _Resolution | None) -> Iterator[None]:
    token = _resolution.set(resolution)
    try:
        yield
    finally:
        _resolution.reset(token)


def load_for_run(agent_id: str, directory: Path) -> tuple[Any, ...]:
    resolution = _resolution.get()
    if resolution is not None and resolution[:2] == (agent_id, directory):
        return resolution[2:]
    from robothor.engine.runner import load_agent_config_or_reason

    return load_agent_config_or_reason(agent_id, directory)
