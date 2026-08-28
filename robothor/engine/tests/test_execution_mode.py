"""Which mode we are in is an observation, not an announcement.

2026-08-27. The fleet spent 29 hours serving every run from the local tier and
nothing in the engine knew. Timeouts, concurrency, the reaper and the cost
governor all went on applying cloud economics to a device-bound workload, and
that mismatch -- not the model -- produced a 33% failure rate.

The tempting implementation is a flag set by whoever notices the outage. These
tests pin the two ways that goes wrong:

* ``KeyPool.exhausted()`` is not a pure read. ``_available()`` deletes the
  retirement record and re-arms once the cooldown elapses, so on a *weekly* cap
  it optimistically reports healthy every 6 hours and immediately re-fails.
  A mode that exits on ``not exhausted()`` would un-defer the fleet four times
  a day and stampede. Exit is therefore driven by an observed cloud success and
  nothing else -- ``test_time_alone_never_leaves_local`` is that guarantee.
* A provider nobody uses running out of credit must not repolicy the fleet.

Entering LOCAL because the credential capped and entering it because the
operator chose to are the same state with the same rules: an instance that
deliberately runs local-only is not in an outage.
"""

import pytest

from robothor.engine.execution_mode import (
    DEFAULT_MIN_DWELL_SECONDS,
    DEFAULT_QUIET_WINDOW_SECONDS,
    LOCAL_STREAK_TO_ENTER,
    ExecutionMode,
    ExecutionModeTracker,
)

CLOUD_MODEL = "openrouter/openai/ox-alpha"
LOCAL_MODEL = "ollama_chat/qwen3.8:27b"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def tracker(clock, monkeypatch):
    monkeypatch.delenv("ROBOTHOR_EXECUTION_MODE", raising=False)
    return ExecutionModeTracker(clock=clock)


def _go_local(tracker):
    for _ in range(LOCAL_STREAK_TO_ENTER):
        tracker.record_completion(LOCAL_MODEL)


class TestEnteringLocal:
    def test_a_fresh_engine_is_cloud(self, tracker):
        """Fail toward today's behaviour: no evidence means no repolicy."""
        assert tracker.mode() is ExecutionMode.CLOUD

    def test_one_local_completion_is_not_an_outage(self, tracker):
        tracker.record_completion(LOCAL_MODEL)
        assert tracker.mode() is ExecutionMode.CLOUD

    def test_a_streak_of_local_completions_enters_local(self, tracker):
        _go_local(tracker)
        assert tracker.mode() is ExecutionMode.LOCAL

    def test_a_cloud_completion_breaks_the_streak(self, tracker):
        tracker.record_completion(LOCAL_MODEL)
        tracker.record_completion(LOCAL_MODEL)
        tracker.record_completion(CLOUD_MODEL)
        tracker.record_completion(LOCAL_MODEL)
        assert tracker.mode() is ExecutionMode.CLOUD


class TestLeavingLocalRequiresEvidence:
    def test_time_alone_never_leaves_local(self, tracker, clock):
        """The weekly-cap trap: a pool that flips healthy every 6h proves nothing."""
        _go_local(tracker)
        clock.advance(60 * 60 * 24)
        assert tracker.mode() is ExecutionMode.LOCAL

    def test_a_cloud_success_alone_is_not_enough_before_the_dwell(self, tracker, clock):
        _go_local(tracker)
        tracker.record_completion(CLOUD_MODEL)
        assert tracker.mode() is ExecutionMode.LOCAL

    def test_a_cloud_success_past_dwell_and_quiet_returns_to_cloud(self, tracker, clock):
        _go_local(tracker)
        clock.advance(DEFAULT_MIN_DWELL_SECONDS + 1)
        tracker.record_completion(CLOUD_MODEL)
        clock.advance(DEFAULT_QUIET_WINDOW_SECONDS + 1)
        assert tracker.mode() is ExecutionMode.CLOUD

    def test_a_local_completion_during_the_quiet_window_cancels_the_return(self, tracker, clock):
        """Flapping back mid-outage is worse than staying put."""
        _go_local(tracker)
        clock.advance(DEFAULT_MIN_DWELL_SECONDS + 1)
        tracker.record_completion(CLOUD_MODEL)
        tracker.record_completion(LOCAL_MODEL)
        clock.advance(DEFAULT_QUIET_WINDOW_SECONDS + 1)
        assert tracker.mode() is ExecutionMode.LOCAL


class TestOperatorOverride:
    def test_local_only_is_a_first_class_configuration_not_an_alarm(self, clock, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EXECUTION_MODE", "local")
        t = ExecutionModeTracker(clock=clock)
        assert t.mode() is ExecutionMode.LOCAL
        for _ in range(10):
            t.record_completion(CLOUD_MODEL)
        assert t.mode() is ExecutionMode.LOCAL

    def test_pinning_cloud_ignores_local_evidence(self, clock, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EXECUTION_MODE", "cloud")
        t = ExecutionModeTracker(clock=clock)
        _go_local(t)
        assert t.mode() is ExecutionMode.CLOUD

    def test_auto_is_evidence_driven(self, clock, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EXECUTION_MODE", "auto")
        t = ExecutionModeTracker(clock=clock)
        _go_local(t)
        assert t.mode() is ExecutionMode.LOCAL

    def test_a_typo_falls_back_to_auto_rather_than_wedging_the_fleet(self, clock, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_EXECUTION_MODE", "locl")
        t = ExecutionModeTracker(clock=clock)
        assert t.mode() is ExecutionMode.CLOUD
        _go_local(t)
        assert t.mode() is ExecutionMode.LOCAL


class TestObservability:
    def test_the_snapshot_explains_itself(self, tracker):
        _go_local(tracker)
        snap = tracker.snapshot()
        assert snap["mode"] == "local"
        assert snap["last_model"] == LOCAL_MODEL
        assert snap["source"] in ("observed", "override")
