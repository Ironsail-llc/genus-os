"""The probe has to be able to fail, or it is decoration with a green tick.

Every prior incident on this control ended the same way: a mechanism that could
not act, and a test asserting the state it could not act in was correct. So
this suite does not only run the check and read `pass`; it neutralises the
guard three different ways and requires the check to say so each time.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from robothor.doctor.checks import step_efficiency as step_checks
from robothor.doctor.tests.conftest import make_ctx

if TYPE_CHECKING:
    import pytest


def _run() -> Any:
    check = next(c for c in step_checks.CHECKS if c.id == "step_efficiency.guard")
    return asyncio.run(check.run(make_ctx()))


def _allow_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe is about the guard, not about how this box seeds roles."""
    import robothor.engine.permissions as perms

    monkeypatch.setattr(perms, "check_tool_permission", lambda *a, **kw: None)


def test_the_probe_passes_against_a_working_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_every_tool(monkeypatch)
    result = _run()
    assert result.status == "pass", result.detail
    assert "answered" in result.detail


def test_it_fails_when_the_guard_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import robothor.engine.repeat_guard as rg

    _allow_every_tool(monkeypatch)
    monkeypatch.setattr(rg.RepeatGuard, "before", lambda self, *a, **kw: None)
    result = _run()
    assert result.status == "fail"
    assert "decided nothing" in result.detail


def test_it_fails_when_every_repeat_resends_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured defect: the guard answers, records a `warned` row, and
    hands back the same bytes the tool would have."""
    import robothor.engine.repeat_guard as rg

    _allow_every_tool(monkeypatch)
    monkeypatch.setattr(rg.RepeatGuard, "_remember_answer", lambda self, *a, **kw: None)
    result = _run()
    assert result.status == "fail"
    assert "saves nothing" in result.detail


def test_it_fails_when_the_guard_points_at_content_that_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite failure, and the worse one: a pointer to a result
    compaction removed leaves the model unable to see a file it is holding a
    reference to."""
    import robothor.engine.repeat_guard as rg

    _allow_every_tool(monkeypatch)
    monkeypatch.setattr(rg.RepeatGuard, "_still_in_context", lambda self, payload: True)
    result = _run()
    assert result.status == "fail"
    assert "can no longer see" in result.detail


def test_a_denied_tool_call_is_a_skip_not_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A green line for something nobody could check is how this control got
    its reputation."""
    import robothor.engine.permissions as perms

    monkeypatch.setattr(perms, "check_tool_permission", lambda *a, **kw: "nope")
    result = _run()
    assert result.status == "skip"


def test_the_check_is_registered_and_writes_no_evidence_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that landed rows in `agent_guardrail_events` would show up in
    the sweep that reads them as agent activity that never happened."""
    import robothor.engine.tracking as tracking
    from robothor.doctor.registry import builtin_ids

    assert "step_efficiency.guard" in builtin_ids()

    _allow_every_tool(monkeypatch)
    rows: list[Any] = []
    monkeypatch.setattr(tracking, "log_guardrail_event", lambda *a, **kw: rows.append(kw))
    assert _run().status == "pass"
    assert rows == []
