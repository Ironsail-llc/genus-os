"""Trusted workflow requirements for the next provider tool selection.

A requirement selects an already granted tool. It never grants a capability or
accepts authority from model messages, and its owner still validates the result.
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class _Requirement:
    name: str
    pending: Callable[[], bool]
    active: bool = True


_current: ContextVar[_Requirement | None] = ContextVar("required_tool", default=None)


@contextmanager
def required_tool_scope(name, pending):
    if name is not None and (not isinstance(name, str) or not name or not callable(pending)):
        raise ValueError("A required tool needs a name and a trusted pending predicate")
    requirement = _Requirement(name, pending) if name is not None else None
    token = _current.set(requirement)
    try:
        yield
    finally:
        if requirement is not None:
            requirement.active = False
        _current.reset(token)


def tool_choice(tools):
    requirement = _current.get()
    if requirement is None or not requirement.active or not requirement.pending():
        return "auto"
    if not any(tool.get("function", {}).get("name") == requirement.name for tool in tools):
        raise ValueError("Required workflow tool is not available in this request")
    return {"type": "function", "function": {"name": requirement.name}}


def endpoint_tool_contract(kwargs, endpoint):
    """Equivalent forced-call payload, or None if this endpoint cannot honor it."""
    from copy import deepcopy

    choice = kwargs.get("tool_choice")
    kind = "function" if isinstance(choice, dict) else choice
    if kind not in ("function", "required"):
        return {}
    capabilities = endpoint.get("supports_tool_choice")
    if not {"tools", "tool_choice"} <= set(
        endpoint.get("supported_parameters", [])
    ) or not isinstance(capabilities, dict):
        return None
    if capabilities.get(kind) is True:
        return {}
    if kind == "function" and capabilities.get("required") is True:
        name = (choice.get("function") or {}).get("name")
        selected = [
            tool
            for tool in kwargs.get("tools", [])
            if tool.get("type") == "function" and tool.get("function", {}).get("name") == name
        ]
        if isinstance(name, str) and name and len(selected) == 1:
            return {"tool_choice": "required", "tools": deepcopy(selected)}
    return None
