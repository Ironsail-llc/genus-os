"""``tool_call`` accepts the argument shapes models actually send.

With deferral on by default, ``tool_call`` is the normal road to every tool
outside an agent's core. Models often serialise the wrapped call's
``arguments`` as a JSON string rather than an object; goal admission rejected
that with a bare ``ValueError`` that dispatch reported as a tool crash, so the
deferred tool never ran. A JSON string of an object is the same call; a
wrapper that is genuinely malformed is a bad call the model can fix, not a
crash, and the refusal says how to send it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from robothor.engine.tools import dispatch
from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed

if TYPE_CHECKING:
    from collections.abc import Iterator

GRANTED = "list_widgets"


@pytest.fixture
def inner(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Record what the wrapped tool is run with, on a deferred run granting it."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
        seen.append((name, args))
        return {"ok": True, "ran": name}

    registry = SimpleNamespace(execute=execute)
    monkeypatch.setattr(dispatch, "_audit_tool_call", lambda *a, **k: None)
    monkeypatch.setattr("robothor.engine.tools.registry.get_registry", lambda: registry)
    token = set_deferred_allowed(frozenset({GRANTED}))
    try:
        yield seen
    finally:
        clear_deferred_allowed(token)


async def _call(arguments: dict[str, Any]) -> dict[str, Any]:
    return await dispatch._execute_tool("tool_call", arguments, user_role="service")


async def test_json_string_arguments_run_the_wrapped_tool(inner: list[Any]) -> None:
    result = await _call({"name": GRANTED, "arguments": json.dumps({"limit": 3})})
    assert result == {"ok": True, "ran": GRANTED}
    assert inner == [(GRANTED, {"limit": 3})]


async def test_object_arguments_still_run_the_wrapped_tool(inner: list[Any]) -> None:
    result = await _call({"name": GRANTED, "arguments": {"limit": 3}})
    assert result == {"ok": True, "ran": GRANTED}
    assert inner == [(GRANTED, {"limit": 3})]


async def test_missing_arguments_mean_no_arguments(inner: list[Any]) -> None:
    assert await _call({"name": GRANTED}) == {"ok": True, "ran": GRANTED}
    assert inner == [(GRANTED, {})]


@pytest.mark.parametrize(
    ("wrapper", "says"),
    [
        ({"name": "tool_call", "arguments": {"name": GRANTED}}, "tool_call cannot wrap"),
        ({"arguments": {}}, "name"),
        ({"name": 7, "arguments": {}}, "name"),
        ({"name": GRANTED, "arguments": "not json"}, "arguments"),
        ({"name": GRANTED, "arguments": json.dumps([1, 2])}, "arguments"),
        ({"name": GRANTED, "arguments": 5}, "arguments"),
    ],
)
async def test_malformed_wrapper_is_a_clean_refusal_not_a_crash(
    inner: list[Any], wrapper: dict[str, Any], says: str
) -> None:
    result = await _call(wrapper)
    assert "tool_crashed" not in result
    error = result["error"]
    assert says in error
    assert "ValueError" not in error
    # It says how to send it.
    assert "object" in error
    assert "tool_describe" in error
    assert inner == []


def test_admission_reads_a_json_string_wrapper_as_its_object() -> None:
    """Goal admission judges the wrapped call, whatever shape it came in."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.goals import runtime

    with patch.object(runtime, "binding") as current:
        current.get.return_value = None
        # No exception: the string parses to the object the dict form carries.
        runtime.admit_tool(
            "tool_call", {"name": GRANTED, "arguments": json.dumps({"limit": 3})}, ToolContext()
        )


def test_read_only_classification_sees_through_a_json_string() -> None:
    from robothor.engine.wrapped_call import inner_call, is_read_only_call

    assert inner_call("tool_call", {"name": "x", "arguments": '{"a": 1}'}) == ("x", {"a": 1})
    assert is_read_only_call(
        "tool_call",
        {"name": "pr_review_intake", "arguments": json.dumps({"count_only": True})},
        read_only=(),
    )
