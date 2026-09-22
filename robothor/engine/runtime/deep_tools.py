"""Bind deep tool callbacks to their trusted owner across framework threads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from contextvars import ContextVar, copy_context
from functools import wraps

owner: ContextVar[tuple[str, str] | None] = ContextVar("deep_tool_owner", default=None)


def bind_tools(definitions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    identity = owner.get()
    if identity is None:
        return definitions
    context = copy_context()

    def bind(function: Callable[..., Any]) -> Callable[..., Any]:
        def dispatch(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            from robothor.engine.runtime.controls import stopped
            from robothor.engine.runtime.provider_budget import DurableStopError

            if stopped(*identity):
                raise DurableStopError("Durable stop denies another deep tool call")
            return function(*args, **kwargs)

        @wraps(function)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            # A context cannot be entered concurrently. Copy the captured owner
            # context per invocation, including when the framework uses threads.
            return context.copy().run(dispatch, args, kwargs)

        return invoke

    return {
        name: {**definition, "tool": bind(definition["tool"])}
        for name, definition in definitions.items()
    }
