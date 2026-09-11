"""A tool failure must record WHY, not only THAT.

2026-08-27: 68.4% of tool failures in 7 days (976 of 1,426) carried
``error_type='unknown'``, and joining ``agent_tool_events.step_id`` to
``agent_run_steps.error_message`` recovered a reason for **zero** of them.
The platform recorded which tool failed and when, and nothing about why —
so ``check_tool_degradation`` paged ``Tool degradation: create_task`` on a
failure whose cause was written down nowhere, undiagnosable by an operator
or by an investigating agent.

The ordering matters: you cannot widen ``classify_error``'s patterns to
cover traffic you never stored. Reason capture comes first; classification
is downstream of it.
"""

from __future__ import annotations

import robothor.engine.tracking as tracking


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self, *a, **k):
        return _Cur(self.sink)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    sink: list = []
    monkeypatch.setattr(tracking, "get_connection", lambda *a, **k: _Conn(sink))
    return sink


def test_the_failure_reason_is_persisted(monkeypatch):
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="create_task",
        duration_ms=5,
        success=False,
        error_type="unknown",
        error_message="Tool 'create_task' rejected: quota",
    )
    sql, params = sink[0]
    assert "error_message" in sql, "the reason column is not being written"
    assert any("quota" in str(p) for p in params)


def test_a_long_reason_is_truncated(monkeypatch):
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="exec",
        duration_ms=5,
        success=False,
        error_type="unknown",
        error_message="x" * 5000,
    )
    _sql, params = sink[0]
    stored = next(p for p in params if isinstance(p, str) and p.startswith("x"))
    assert len(stored) <= tracking.MAX_TOOL_ERROR_CHARS, (
        "an unbounded tool error would let one pathological payload bloat the table"
    )


def test_a_successful_call_stores_no_reason(monkeypatch):
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="read_file",
        duration_ms=5,
        success=True,
        error_message="should not be kept",
    )
    _sql, params = sink[0]
    assert "should not be kept" not in [p for p in params if isinstance(p, str)]


def test_omitting_the_reason_still_works(monkeypatch):
    """Backwards compatible: existing callers must not break."""
    sink = _capture(monkeypatch)
    tracking.log_tool_event(run_id="r1", tool_name="x", duration_ms=1, success=False)
    assert sink, "call raised or wrote nothing"


def test_an_error_type_enum_is_normalised(monkeypatch):
    """Callers must not need to know ErrorType's shape to log an event."""
    from robothor.engine.error_recovery import ErrorType

    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="x",
        duration_ms=1,
        success=False,
        error_type=ErrorType.TIMEOUT,
    )
    _sql, params = sink[0]
    assert ErrorType.TIMEOUT.value in params


def test_a_bare_string_error_type_still_works(monkeypatch):
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="x",
        duration_ms=1,
        success=False,
        error_type="auth",
    )
    _sql, params = sink[0]
    assert "auth" in params


# ─── Sandbox denials are not tool degradation ────────────────────────
#
# 2026-09-11 15:08: agent_tool_events in the last 75 minutes showed
# create_task 10 ok / 14 failed, every failure's error_message =
# "benchmark sandbox: create_task writes are disabled" — the nightly
# benchmark runner being correctly refused write access, not a broken
# tool. The operator got paged "Tool degradation: create_task" anyway
# because nothing distinguished a sandbox refusal from a real failure at
# the point the row was written.


def test_a_sandbox_denial_is_classified_regardless_of_the_passed_error_type(monkeypatch):
    """The prefix on the message is what marks a row as a sandbox denial —
    not whatever error_type the caller happened to pass (callers upstream
    of this classification, e.g. tool_outcome.py's classify_error, have no
    idea the sandbox exists)."""
    from robothor.constants import SANDBOX_DENIAL_PREFIX

    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="create_task",
        duration_ms=5,
        success=False,
        error_type="unknown",
        error_message=f"{SANDBOX_DENIAL_PREFIX} create_task writes are disabled",
    )
    _sql, params = sink[0]
    # params order is run_id, step_id, tool_name, duration_ms, success, error_type, error_message.
    assert params[-2] == "sandbox_denied", "the passed-in 'unknown' must be overridden"
    assert "unknown" not in params


def test_a_successful_call_is_never_classified_as_a_sandbox_denial(monkeypatch):
    """Guard against a too-broad match: success=True must never turn into a
    sandbox_denied row, whatever the (unused) message says."""
    from robothor.constants import SANDBOX_DENIAL_PREFIX

    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="create_task",
        duration_ms=5,
        success=True,
        error_message=f"{SANDBOX_DENIAL_PREFIX} create_task writes are disabled",
    )
    _sql, params = sink[0]
    assert "sandbox_denied" not in params


def test_a_failure_with_no_message_falls_back_to_the_error_type(monkeypatch):
    """The guardrail-block path (tool_admission.py) historically logged
    success=False with an error_type but no error_message at all — the row
    said THAT write_file failed and nothing about why."""
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="write_file",
        duration_ms=0,
        success=False,
        error_type="guardrail_blocked",
    )
    _sql, params = sink[0]
    # params order is run_id, step_id, tool_name, duration_ms, success, error_type, error_message.
    # Both the type column AND the reason column must carry it — a match on
    # error_type alone (present regardless of this fix) would pass vacuously.
    assert params[-2] == "guardrail_blocked", "error_type must still be recorded"
    assert params[-1] == "guardrail_blocked", (
        "an empty error_message must fall back to the error_type, "
        "not stay empty just because no explicit message was given"
    )


def test_a_failure_with_no_message_and_no_type_gets_a_placeholder(monkeypatch):
    sink = _capture(monkeypatch)
    tracking.log_tool_event(
        run_id="r1",
        tool_name="x",
        duration_ms=0,
        success=False,
    )
    _sql, params = sink[0]
    assert "failed without a message" in params
