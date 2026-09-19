"""Trusted workflow-owned response schemas; never parsed from model text."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass


@dataclass
class _Schema:
    response_format: dict
    ready: Callable[[], bool]
    active: bool = True
    defer_for_tools: bool = False


_schema: ContextVar[_Schema | None] = ContextVar("workflow_response_schema", default=None)


@contextmanager
def response_schema_scope(name, schema, *, ready=lambda: True, defer_for_tools=False):
    """Request the trusted schema when ready; callers still validate all output."""
    state = _Schema(
        {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": deepcopy(schema),
            },
        },
        ready,
        defer_for_tools=defer_for_tools,
    )
    token = _schema.set(state)
    try:
        yield
    finally:
        state.active = False
        _schema.reset(token)


def response_format():
    state = _schema.get()
    if state is None or not state.active or not state.ready():
        return None
    return deepcopy(state.response_format)


def defers_tool_turns():
    """Collection workflows validate final JSON in code while allowing more tools."""
    state = _schema.get()
    return state is not None and state.active and state.defer_for_tools
