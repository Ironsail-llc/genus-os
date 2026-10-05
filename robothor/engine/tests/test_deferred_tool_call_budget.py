"""On a deferred run, ``tool_call`` gets the budget of the tool it runs.

Observed 2026-10-05: an agent with more tools than the deferral threshold
called ``tool_call(name="claude_code_wait", arguments={"timeout_s": 1200})``
and was cut at 120 s — "Tool 'tool_call' timed out after 120s" — because the
deadline was resolved for the wrapper's name, never for the tool it wrapped.
The same path is how ``main`` (deferred, ~117 tools) drives coding jobs from
Telegram, so every wait longer than two minutes died there too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from robothor.engine.tool_timeouts import resolve_tool_timeout


def test_tool_call_gets_the_inner_self_timed_tools_budget() -> None:
    wrapped = resolve_tool_timeout(
        "tool_call", 120, {"name": "claude_code_wait", "arguments": {"timeout_s": 1200}}
    )
    assert wrapped == resolve_tool_timeout("claude_code_wait", 120)
    assert wrapped > 1200


def test_tool_call_gets_the_inner_long_running_and_harness_budgets() -> None:
    assert resolve_tool_timeout("tool_call", 120, {"name": "pr_review_prepare"}) == 600
    assert resolve_tool_timeout("tool_call", 120, {"name": "benchmark_run_fleet"}) == 0


def test_tool_call_wrapping_an_ordinary_tool_keeps_the_configured_cap() -> None:
    assert resolve_tool_timeout("tool_call", 120, {"name": "search_memory"}) == 120
    assert resolve_tool_timeout("tool_call", 120, {}) == 120
    assert resolve_tool_timeout("tool_call", 120, {"name": 7}) == 120
    assert resolve_tool_timeout("tool_call", 120, None) == 120
    # A nested meta-tool is refused by the handler; it earns no budget here.
    assert resolve_tool_timeout("tool_call", 120, {"name": "tool_call"}) == 120


def test_inner_call_looks_through_the_wrapper_only() -> None:
    from robothor.engine.wrapped_call import inner_call

    assert inner_call("tool_call", {"name": " x ", "arguments": {"a": 1}}) == ("x", {"a": 1})
    assert inner_call("tool_call", {"name": "x", "arguments": "bad"}) == ("x", {})
    assert inner_call("read_file", {"path": "p"}) == ("read_file", {"path": "p"})
    assert inner_call("tool_call", None) == ("tool_call", {})


def _request() -> Any:
    run = SimpleNamespace(
        id="r",
        tenant_id="t",
        user_id="u",
        user_role="service",
        accessible_tenant_ids=(),
        is_benchmark=False,
    )
    return SimpleNamespace(
        on_tool=None,
        trace=None,
        agent_config=SimpleNamespace(id="a", tool_timeout_seconds=120, task_author_override=""),
        session=SimpleNamespace(run=run, identity=None),
    )


async def test_tool_turn_applies_the_inner_budget_to_tool_call() -> None:
    """The live dispatch path, not only the resolver."""
    from robothor.engine.tool_turn import ToolTurnMixin

    seen: dict[str, int] = {}

    async def execute(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen[name] = kwargs["timeout"]
        return {"ok": True}

    executor = ToolTurnMixin.__new__(ToolTurnMixin)
    executor.registry = SimpleNamespace(execute=execute)  # type: ignore[attr-defined]
    executor.config = SimpleNamespace(workspace="/tmp")  # type: ignore[attr-defined]
    call = SimpleNamespace(
        tool_name="tool_call",
        tool_args={"name": "claude_code_wait", "arguments": {"timeout_s": 1200}},
        tc=SimpleNamespace(id="c1"),
        result=None,
        elapsed_ms=0,
    )
    await executor._execute_one(call, _request())  # type: ignore[arg-type]
    assert seen["tool_call"] == resolve_tool_timeout("claude_code_wait", 120)


async def test_tool_call_handler_does_not_reimpose_a_default_inner_deadline(
    monkeypatch: Any,
) -> None:
    """The handler's inner ``registry.execute`` defaulted to 120 s: a second,
    smaller deadline under the one the dispatcher resolved for the inner tool.
    The outer deadline owns the budget, so the inner one is 0 (unlimited)."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import toolsearch

    seen: dict[str, int] = {}

    async def execute(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen[name] = kwargs.get("timeout", 120)
        return {"ok": True}

    monkeypatch.setattr(toolsearch, "_allowed_names", lambda: ["claude_code_wait"])
    monkeypatch.setattr(
        "robothor.engine.tools.registry.get_registry",
        lambda: SimpleNamespace(execute=execute),
    )
    result = await toolsearch._tool_call(
        {"name": "claude_code_wait", "arguments": {"job_id": "j"}}, ToolContext()
    )
    assert result == {"ok": True}
    assert seen["claude_code_wait"] == 0
