"""Recording what one tool call did: classify, log, record, trip the breaker.

Extracted from the tool-execution block inside `_run_loop` — the largest and
most entangled cluster left in the god-object the competitive analysis puts
first. Four steps that share one subject (the call that just finished) and one
rule: none of them may take the run down.

Two details were load-bearing and untested:

* the scratchpad is given the RESULT and the ARGS, not just the tool name.
  They feed the no-progress detector; without them every call looks identical
  and the detector can never fire.
* the circuit breaker trips at the third failure of the same tool, and speaks
  to the agent rather than raising. An agent that keeps calling a broken tool
  burns its whole budget on it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from robothor.engine.tool_outcome import record_tool_outcome


def _session():
    return SimpleNamespace(
        run=SimpleNamespace(id="run-1"),
        messages=[],
    )


class FakeScratchpad:
    def __init__(self):
        self.calls = []

    def record_tool_call(self, tool_name, error=None, result=None, tool_input=None):
        self.calls.append((tool_name, error, result, tool_input))


def _record(session, **kw):
    return record_tool_outcome(
        session,
        tool_name=kw.pop("tool_name", "exec"),
        tool_args=kw.pop("tool_args", {"cmd": "ls"}),
        result=kw.pop("result", {"ok": True}),
        error_msg=kw.pop("error_msg", None),
        elapsed_ms=kw.pop("elapsed_ms", 12),
        scratchpad=kw.pop("scratchpad", None),
        failures=kw.pop("failures", {}),
    )


# ── Classification ────────────────────────────────────────────────────


def test_a_successful_call_has_no_error_type():
    with patch("robothor.engine.tracking.log_tool_event"):
        assert _record(_session()) is None


def test_a_failed_call_is_classified_and_the_type_returned():
    """The type is consumed downstream by escalation, so returning it is the
    contract, not a convenience."""
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="TIMEOUT") as cls,
    ):
        assert _record(_session(), error_msg="timed out") == "TIMEOUT"

    cls.assert_called_once_with("exec", "timed out")


# ── Observability ─────────────────────────────────────────────────────


def test_the_call_is_logged_with_its_duration_and_outcome():
    with patch("robothor.engine.tracking.log_tool_event") as log:
        _record(_session(), elapsed_ms=345)

    kwargs = log.call_args.kwargs
    assert kwargs["tool_name"] == "exec"
    assert kwargs["duration_ms"] == 345
    assert kwargs["success"] is True


def test_a_logging_failure_never_breaks_the_run():
    """Observability is not worth the work the agent has already done."""
    with patch("robothor.engine.tracking.log_tool_event", side_effect=RuntimeError("db down")):
        assert _record(_session(), error_msg=None) is None


# ── The scratchpad's no-progress detector ─────────────────────────────


def test_the_scratchpad_is_given_the_result_and_the_args():
    """Without them every call looks identical and the no-progress detector
    can never fire — the whole reason it is passed more than a tool name."""
    pad = FakeScratchpad()
    with patch("robothor.engine.tracking.log_tool_event"):
        _record(_session(), scratchpad=pad, result={"rows": 3}, tool_args={"q": "x"})

    name, error, result, args = pad.calls[0]
    assert name == "exec" and error is None
    assert result == {"rows": 3}
    assert args == {"q": "x"}


def test_a_failing_scratchpad_does_not_break_the_run():
    pad = SimpleNamespace(record_tool_call=lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with patch("robothor.engine.tracking.log_tool_event"):
        _record(_session(), scratchpad=pad)


def test_no_scratchpad_is_fine():
    with patch("robothor.engine.tracking.log_tool_event"):
        _record(_session(), scratchpad=None)


# ── Circuit breaker ───────────────────────────────────────────────────


def test_the_breaker_counts_failures_per_tool():
    failures = {}
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="X"),
    ):
        _record(_session(), error_msg="boom", failures=failures)
        _record(_session(), error_msg="boom", failures=failures)

    assert failures["exec"] == 2


def test_two_failures_do_not_trip_it():
    session = _session()
    failures = {}
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="X"),
    ):
        for _ in range(2):
            _record(session, error_msg="boom", failures=failures)

    assert session.messages == []


def test_the_third_failure_tells_the_agent_to_stop_calling_it():
    """An agent that keeps calling a broken tool burns its whole budget on it."""
    session = _session()
    failures = {}
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="X"),
    ):
        for _ in range(3):
            _record(session, error_msg="boom", failures=failures)

    assert len(session.messages) == 1
    content = session.messages[0]["content"]
    assert "exec" in content and "Do NOT call it again" in content


def test_the_nudge_repeats_while_the_agent_keeps_calling_the_broken_tool():
    """Pinned rather than assumed: an agent that called the tool again after
    being told not to is the one that needs telling again."""
    session = _session()
    failures = {}
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="X"),
    ):
        for _ in range(5):
            _record(session, error_msg="boom", failures=failures)

    assert len(session.messages) == 3, "failures 3, 4 and 5 each earn a nudge"


def test_the_breaker_is_scoped_to_one_tool():
    """A broken `exec` must not silence `read_file`."""
    session = _session()
    failures = {}
    with (
        patch("robothor.engine.tracking.log_tool_event"),
        patch("robothor.engine.error_recovery.classify_error", return_value="X"),
    ):
        for _ in range(3):
            _record(session, tool_name="exec", error_msg="boom", failures=failures)
        _record(session, tool_name="read_file", error_msg="boom", failures=failures)

    assert failures == {"exec": 3, "read_file": 1}
    assert len(session.messages) == 1


def test_a_success_does_not_count_against_the_breaker():
    failures = {}
    with patch("robothor.engine.tracking.log_tool_event"):
        _record(_session(), error_msg=None, failures=failures)

    assert failures == {}
