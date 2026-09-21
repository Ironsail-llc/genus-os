"""Bind deep tool callbacks to their trusted owner across framework threads."""

from contextvars import ContextVar, copy_context
from functools import wraps

owner: ContextVar[tuple[str, str] | None] = ContextVar("deep_tool_owner", default=None)


def bind_tools(definitions):
    identity = owner.get()
    if identity is None:
        return definitions
    context = copy_context()

    def bind(function):
        def dispatch(args, kwargs):
            from robothor.engine.runtime.controls import stopped
            from robothor.engine.runtime.provider_budget import DurableStopError

            if stopped(*identity):
                raise DurableStopError("Durable stop denies another deep tool call")
            return function(*args, **kwargs)

        @wraps(function)
        def invoke(*args, **kwargs):
            # A context cannot be entered concurrently. Copy the captured owner
            # context per invocation, including when the framework uses threads.
            return context.copy().run(dispatch, args, kwargs)

        return invoke

    return {
        name: {**definition, "tool": bind(definition["tool"])}
        for name, definition in definitions.items()
    }
