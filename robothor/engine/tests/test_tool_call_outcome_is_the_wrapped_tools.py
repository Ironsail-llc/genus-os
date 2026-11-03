"""A ``tool_call``'s outcome is counted, logged and remembered as the wrapped tool.

Under deferred loading most tools are reached as ``tool_call(name=X, ...)``.
The recorder passed the wrapper's own name to the outcome step, so three
UNRELATED deferred failures (a mail lookup, a ticket lookup, an allow-list
refusal) tripped the circuit breaker for ``tool_call`` and the agent was told
never to call it again: cut off from every deferred tool at once. The same
name fed the tool-event rows, the per-kind error counter and the scratchpad's
no-progress detector. Every one of them now sees the wrapped tool; a wrapper
that names no runnable tool is still the wrapper.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine import tool_turn
from robothor.engine.tool_turn import ToolTurnMixin, _Call


class _Session:
    def __init__(self) -> None:
        self.run = SimpleNamespace(id="run-1", total_cost_usd=0.0)
        self.messages: list[dict[str, Any]] = []
        self.todo_list = None

    def record_tool_call(self, **_kwargs: Any) -> None:
        return None


class _Scratchpad:
    def __init__(self) -> None:
        self.names: list[str] = []

    def record_tool_call(self, name: str, **_kwargs: Any) -> None:
        self.names.append(name)


class _Escalation:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def record_error(self, _kind: Any) -> None:
        return None

    def record_error_kind(self, name: str, _msg: str) -> None:
        self.kinds.append(name)

    def record_success(self) -> None:
        return None


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    logged: list[str] = []
    monkeypatch.setattr(
        "robothor.engine.tracking.log_tool_event",
        lambda **kw: logged.append(kw["tool_name"]),
    )
    monkeypatch.setattr(
        tool_turn, "apply_post_execution_guardrails", lambda *_a, **kw: kw["result"]
    )
    return logged


def _recorder() -> ToolTurnMixin:
    recorder = ToolTurnMixin.__new__(ToolTurnMixin)
    recorder._active_watchdog = None  # type: ignore[attr-defined]
    return recorder


def _request(session: _Session) -> Any:
    return tool_turn.ToolTurnRequest(
        assistant_msg=None,
        session=session,  # type: ignore[arg-type]
        agent_config=SimpleNamespace(id="a"),  # type: ignore[arg-type]
        iteration=1,
        scratchpad=_Scratchpad(),
        escalation=_Escalation(),
    )


def _failed(index: int, name: str, args: dict[str, Any]) -> _Call:
    return _Call(
        index=index,
        tc=SimpleNamespace(id=f"c{index}"),
        tool_name=name,
        tool_args=args,
        result={"error": f"{args.get('name', name)} failed"},
    )


def _wrapped(index: int, inner: str) -> _Call:
    return _failed(index, "tool_call", {"name": inner, "arguments": {"q": index}})


async def _record_all(calls: list[_Call]) -> tuple[_Session, Any]:
    session = _Session()
    req = _request(session)
    recorder = _recorder()
    for call in calls:
        await recorder._record(call, req, [], batch_id="b")
    return session, req


def _breaker_notes(session: _Session) -> list[str]:
    return [m["content"] for m in session.messages if "Do NOT call it again" in m["content"]]


async def test_three_different_wrapped_failures_do_not_trip_the_breaker(
    events: list[str],
) -> None:
    session, req = await _record_all(
        [_wrapped(0, "gws_gmail_get"), _wrapped(1, "jira_get_issue"), _wrapped(2, "list_tasks")]
    )
    assert _breaker_notes(session) == []
    assert "tool_call" not in req.tool_failures
    assert req.tool_failures == {"gws_gmail_get": 1, "jira_get_issue": 1, "list_tasks": 1}


async def test_three_failures_of_the_same_wrapped_tool_do(events: list[str]) -> None:
    session, _req = await _record_all([_wrapped(i, "gws_gmail_get") for i in range(3)])
    (note,) = _breaker_notes(session)
    assert "'gws_gmail_get'" in note
    assert "'tool_call'" not in note


async def test_event_rows_error_kinds_and_scratchpad_name_the_wrapped_tool(
    events: list[str],
) -> None:
    _session, req = await _record_all([_wrapped(0, "gws_gmail_get")])
    assert events == ["gws_gmail_get"]
    assert req.escalation.kinds == ["gws_gmail_get"]
    assert req.scratchpad.names == ["gws_gmail_get"]


async def test_a_malformed_wrapper_is_still_counted_as_the_wrapper(events: list[str]) -> None:
    _session, req = await _record_all([_failed(0, "tool_call", {"arguments": {}})])
    assert req.tool_failures == {"tool_call": 1}
    assert events == ["tool_call"]
