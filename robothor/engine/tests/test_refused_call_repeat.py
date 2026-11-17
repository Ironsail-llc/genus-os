"""An identical call that was already refused in this run is not made again.

Real-model runs showed that after a refusal the model often re-issued the same
call: the same write, refused again, in two of three runs, and several more
calls to a generic API tool that answered 403. Each retry costs latency and
says nothing new. Within one run, a call identical in tool and arguments to
one that was refused or denied now returns straight away, repeating the
original refusal and saying not to retry.

The scope is narrow on purpose:
- same run only;
- identical arguments only;
- refusals and permission denials only. A plain error or a timeout can
  legitimately succeed on a retry, so it is never short-circuited.

A refusal whose own unlock the model then follows is unaffected. A
deferred tool's "call it through tool_call" goes through the wrapper, which
is a different call.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robothor.engine.refusal import refusal
from robothor.engine.tests.test_tool_call_log import LINE_LOGGER, _fields
from robothor.engine.tools.registry import ToolRegistry

DENIAL = refusal("Role 'member' may not use example_tool", gate="role", name="rbac", unlock="x")
FORBIDDEN = {"status": 403, "body": "not allowed"}


@pytest.fixture(autouse=True)
def _fresh(caplog):
    from robothor.engine import tool_call_log

    getattr(tool_call_log, "_refused_calls", {}).clear()
    caplog.set_level(logging.INFO)
    yield
    getattr(tool_call_log, "_refused_calls", {}).clear()


def _dispatch(answers: dict[str, Any], calls: list[tuple[str, dict[str, Any]]]):
    registry = ToolRegistry.__new__(ToolRegistry)

    async def _execute_tool(name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        calls.append((name, args))
        if name == "tool_call":
            return await ToolRegistry.execute(
                registry, args["name"], args.get("arguments") or {}, timeout=0, **kwargs
            )
        answer = answers[name]
        return answer() if callable(answer) else answer

    return registry, _execute_tool


async def _run(monkeypatch, answers, steps, *, run_id="run-1"):
    calls: list[tuple[str, dict[str, Any]]] = []
    registry, fake = _dispatch(answers, calls)
    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", fake)
    results = []
    for tool, args, *rest in steps:
        rid = rest[0] if rest else run_id
        results.append(
            await ToolRegistry.execute(
                registry, tool, args, agent_id="example-agent", run_id=rid, timeout=0
            )
        )
    return results, calls


def _lines(caplog) -> list[dict[str, str]]:
    return [
        _fields(r.getMessage())
        for r in caplog.records
        if r.name == LINE_LOGGER and "event=tool_call" in r.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [DENIAL, FORBIDDEN], ids=["engine-denial", "http-403"])
async def test_an_identical_refused_call_is_not_made_again(monkeypatch, caplog, answer) -> None:
    args = {"record": "r-1", "note": "hello"}
    results, calls = await _run(
        monkeypatch, {"example_tool": answer}, [("example_tool", args), ("example_tool", args)]
    )
    assert len(calls) == 1, "the repeat reached the tool"
    repeat = results[1]
    assert repeat["refused"] is True
    assert "already refused in this run" in repeat["error"]
    assert "Do not retry" in repeat["error"]
    assert "tell the person" in repeat["error"]
    if "error" in answer:
        assert answer["error"] in repeat["error"]
    line = _lines(caplog)[-1]
    assert line["outcome"] == "refused"
    assert line["reason"] == "repeat of a refused call"


@pytest.mark.asyncio
async def test_argument_order_does_not_make_a_new_call(monkeypatch) -> None:
    _results, calls = await _run(
        monkeypatch,
        {"example_tool": DENIAL},
        [("example_tool", {"a": 1, "b": 2}), ("example_tool", {"b": 2, "a": 1})],
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_different_arguments_are_a_new_call(monkeypatch) -> None:
    _results, calls = await _run(
        monkeypatch,
        {"example_tool": DENIAL},
        [("example_tool", {"record": "r-1"}), ("example_tool", {"record": "r-2"})],
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_another_run_is_not_affected(monkeypatch) -> None:
    _results, calls = await _run(
        monkeypatch,
        {"example_tool": DENIAL},
        [("example_tool", {"a": 1}, "run-1"), ("example_tool", {"a": 1}, "run-2")],
    )
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"error": "connection reset by peer"},
        {"error": "Tool 'example_tool' timed out after 30s."},
        {"status": 503, "body": "busy"},
    ],
    ids=["plain-error", "timeout", "http-503"],
)
async def test_an_error_or_timeout_may_be_retried(monkeypatch, answer) -> None:
    _results, calls = await _run(
        monkeypatch,
        {"example_tool": answer},
        [("example_tool", {"a": 1}), ("example_tool", {"a": 1})],
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_call_without_a_run_is_never_short_circuited(monkeypatch) -> None:
    _results, calls = await _run(
        monkeypatch,
        {"example_tool": DENIAL},
        [("example_tool", {"a": 1}, ""), ("example_tool", {"a": 1}, "")],
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_following_a_deferred_refusals_unlock_still_runs(monkeypatch) -> None:
    """The direct call is refused as deferred; the unlock is to call it through
    tool_call. That call, and the wrapped call it makes, both run."""
    deferred = refusal(
        "'example_tool' is granted but deferred on this run",
        gate="policy",
        name="deferred",
        unlock="call it through tool_call",
    )
    answers = iter([deferred, {"ok": True}])
    args = {"a": 1}
    results, calls = await _run(
        monkeypatch,
        {"example_tool": lambda: next(answers)},
        [
            ("example_tool", args),
            ("tool_call", {"name": "example_tool", "arguments": args}),
        ],
    )
    assert results[1] == {"ok": True}
    assert [name for name, _ in calls] == ["example_tool", "tool_call", "example_tool"]


@pytest.mark.asyncio
async def test_a_refusal_that_names_no_gate_is_a_one_shot_redirection(monkeypatch) -> None:
    """The repeat guard's refusal says "change the call" and lets the next one
    run; it is not a refusal a person grants, so it is never remembered."""
    redirection = {
        "refused": True,
        "reason": "change something first",
        "repeat_guard": "refused_variant",
    }
    answers = iter([redirection, {"stdout": "ok", "exit_code": 0}])
    _results, calls = await _run(
        monkeypatch,
        {"exec": lambda: next(answers)},
        [("exec", {"command": "python test_x.py"}), ("exec", {"command": "python test_x.py"})],
    )
    assert len(calls) == 2
