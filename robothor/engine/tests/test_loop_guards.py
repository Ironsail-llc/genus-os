"""The checks that decide whether the loop may run another iteration.

Extracted from `_run_loop`, which is 1,059 lines inside a 2,938-line file.
The competitive analysis calls that god-object "the disease, not a symptom" and
puts decomposing it first, because it hides correctness bugs — and these guards
are a good example of why: until now they could only be exercised by driving
the entire loop, so their ORDER, which is load-bearing, was never asserted
anywhere.

The order that matters, and the reason:

    wallclock -> steer -> interrupt -> watchdog -> runaway

The wallclock branch does NOT stop the run itself when a watchdog exists. It
TRIPS the watchdog and falls through, so the watchdog branch below is what ends
the run — which makes `execute()` map it to TIMEOUT exactly as if the watchdog
had fired on its own. Reordering these, or making wallclock return directly,
silently changes a run's terminal state from TIMEOUT to ERROR.

On 2026-08-25 a run blew through its 1200s ceiling to 3110s with three layers
silent at once. This check is the loop reading its own clock: it cannot be
cancelled, starved or unhooked without also ending the loop it is part of.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from robothor.engine.loop_guards import GuardState, check_iteration_guards


class FakeWatchdog:
    def __init__(self, abort=False, reason=""):
        self._abort = abort
        self.abort_reason = reason
        self.last_activity_desc = "calling a tool"
        self.tripped_with: str | None = None

    def trip(self, reason):
        self.tripped_with = reason
        self._abort = True
        self.abort_reason = reason

    @property
    def should_abort(self):
        return self._abort


def _session(**kw):
    run = SimpleNamespace(
        input_tokens=kw.pop("input_tokens", 0),
        output_tokens=kw.pop("output_tokens", 0),
        outcome_notes=kw.pop("outcome_notes", ""),
        model_used="test-model",
        total_cost_usd=0.0,
        budget_exhausted=False,
    )
    s = SimpleNamespace(
        run=run,
        run_id="run-1",
        messages=[],
        errors=[],
        interrupted_with=None,
        _steer=kw.pop("steer", None),
        _interrupt=kw.pop("interrupt", None),
    )
    s.consume_pending_steer = lambda: s.__dict__.pop("_steer", None) or None
    s.consume_interrupt = lambda: (
        s.__dict__.pop("_interrupt", "__none__") if "_interrupt" in s.__dict__ else None
    )
    s.record_error = lambda r: s.errors.append(r)
    s.mark_interrupted = lambda note: setattr(s, "interrupted_with", note)
    return s


def _call(session, *, watchdog=None, deadline=None, ceiling=1200, state=None):
    return check_iteration_guards(
        session,
        SimpleNamespace(id="probe"),
        watchdog=watchdog,
        wallclock_deadline=deadline,
        wallclock_ceiling=ceiling,
        state=state or GuardState(),
    )


# ── The quiet path ───────────────────────────────────────────────────


def test_a_healthy_iteration_is_not_stopped():
    assert _call(_session()) is False


def test_no_deadline_configured_never_stops_on_time():
    assert _call(_session(), deadline=None, ceiling=0) is False


# ── Wallclock, and the ordering it depends on ────────────────────────


def test_passing_the_deadline_trips_the_watchdog_rather_than_stopping_directly():
    """So execute() maps the run to TIMEOUT, not ERROR. If this branch ever
    returns on its own, the terminal state changes silently."""
    dog = FakeWatchdog()
    session = _session()

    assert _call(session, watchdog=dog, deadline=time.monotonic() - 1) is True
    assert dog.tripped_with and "1200s" in dog.tripped_with
    # The error reaches the session through the WATCHDOG branch, carrying the
    # watchdog's own reason — not from an independent record_error in the
    # wallclock branch. That routing is what makes execute() classify the run
    # as TIMEOUT rather than ERROR.
    assert session.errors == [dog.abort_reason]


def test_the_trip_reason_names_the_last_activity():
    """'timed out' with no context sends the operator to the transcript."""
    dog = FakeWatchdog()
    _call(_session(), watchdog=dog, deadline=time.monotonic() - 1)

    assert "calling a tool" in dog.tripped_with


def test_without_a_watchdog_the_deadline_records_its_own_error():
    session = _session()

    assert _call(session, watchdog=None, deadline=time.monotonic() - 1) is True
    assert session.errors and "1200s" in session.errors[0]


def test_a_deadline_in_the_future_does_not_stop():
    assert _call(_session(), deadline=time.monotonic() + 600) is False


# ── Operator input ───────────────────────────────────────────────────


def test_a_steer_is_injected_and_the_loop_continues():
    session = _session(steer="focus on the invoice")

    assert _call(session) is False
    assert any("operator steering update" in m["content"] for m in session.messages)
    assert "focus on the invoice" in session.messages[-1]["content"]


def test_an_interrupt_stops_the_run_and_marks_it_interrupted():
    session = _session(interrupt="stop please")

    assert _call(session) is True
    assert session.interrupted_with and "stop please" in session.interrupted_with
    assert session.run.outcome_notes


def test_an_interrupt_with_no_text_still_stops_and_still_explains_itself():
    """The operator may halt without typing anything; the run must not read as
    a mysterious cancellation."""
    session = _session(interrupt="")

    assert _call(session) is True
    assert "interrupted by operator" in (session.interrupted_with or "").lower()


def test_an_interrupt_is_not_recorded_as_an_error():
    """CANCELLED, not FAILED — the distinction the verifier keys on."""
    session = _session(interrupt="stop")
    _call(session)

    assert session.errors == []


# ── Watchdog ─────────────────────────────────────────────────────────


def test_a_flagged_watchdog_stops_the_run():
    session = _session()

    assert _call(session, watchdog=FakeWatchdog(abort=True, reason="stalled 900s")) is True
    assert session.errors == ["stalled 900s"]


# ── Runaway tokens ───────────────────────────────────────────────────


def test_the_hard_cap_stops_the_run_and_marks_the_budget_exhausted():
    from robothor.engine.runner import RUNAWAY_TOKEN_HARD_CAP

    session = _session(input_tokens=RUNAWAY_TOKEN_HARD_CAP, output_tokens=0)

    assert _call(session) is True
    assert session.run.budget_exhausted is True
    assert session.errors and "runaway_token_cap_hit" in session.errors[0]


def test_the_cap_counts_input_and_output_together():
    from robothor.engine.runner import RUNAWAY_TOKEN_HARD_CAP

    half = RUNAWAY_TOKEN_HARD_CAP // 2 + 1
    assert _call(_session(input_tokens=half, output_tokens=half)) is True


def test_the_soft_alert_fires_once_and_does_not_stop_the_run():
    from robothor.engine.runner import RUNAWAY_TOKEN_ALERT

    state = GuardState()
    session = _session(input_tokens=RUNAWAY_TOKEN_ALERT)

    assert _call(session, state=state) is False
    assert state.runaway_alerted is True

    # A second pass must not alert again: the loop runs this every iteration.
    session2 = _session(input_tokens=RUNAWAY_TOKEN_ALERT)
    assert _call(session2, state=state) is False


def test_below_the_alert_threshold_nothing_happens():
    state = GuardState()
    assert _call(_session(input_tokens=10), state=state) is False
    assert state.runaway_alerted is False


# ── The order itself ─────────────────────────────────────────────────


def test_an_interrupt_wins_over_a_runaway_alert():
    """Both are true in the same pass. The operator's halt is the one that
    should describe the run's ending."""
    from robothor.engine.runner import RUNAWAY_TOKEN_ALERT

    session = _session(interrupt="halt", input_tokens=RUNAWAY_TOKEN_ALERT)

    assert _call(session) is True
    assert session.interrupted_with
    assert session.errors == []


def test_a_steer_is_consumed_even_when_the_run_then_stops():
    """Otherwise the steer silently survives into a resumed run and is applied
    at a moment the operator never chose."""
    session = _session(steer="do X", interrupt="halt")

    _call(session)

    assert any("do X" in m["content"] for m in session.messages)
    assert session.consume_pending_steer() is None
