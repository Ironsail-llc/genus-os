"""Durability must not be inversely correlated with how much you need it.

`CHECKPOINT_INTERVAL = 5` checkpoints every 5 successful TOOL CALLS. Measured
on this instance over 30 days:

    benchmark-runner  completed: 107 min avg, 1.2 tool calls avg (max 2)
                      timeout:    49 min avg, 1.7 tool calls avg (max 4)

It runs for 107 minutes making 1.2 tool calls, so it never reaches 5 and never
checkpoints — the time is spent INSIDE one tool call running the suite. Every
long-running agent has that shape (crm-enrichment 44 min / 0.0 calls,
devops-analyst 31 min / 0.0, engine-report 33 min / 1.5).

The consequence is measured, not theoretical: of 45 benchmark-runner runs the
reaper killed with reap_category='daemon_restart', **zero** had a checkpoint to
resume from. Fleet-wide, 6 of 64. So #413's resume feature — correct, tested,
merged — can recover almost nothing, because the agents that need it most are
exactly the ones that never checkpoint.
"""

from __future__ import annotations

from robothor.engine.checkpoint import CHECKPOINT_INTERVAL, CheckpointManager


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_long_run_with_one_tool_call_still_checkpoints():
    """benchmark-runner's exact shape: 107 minutes, one tool call."""
    clock = FakeClock()
    cp = CheckpointManager(run_id="r1", clock=clock)
    cp.record_success()

    assert not cp.should_checkpoint(), "should not fire immediately"
    clock.advance(107 * 60)
    assert cp.should_checkpoint(), (
        "107 minutes of work with one tool call produced no checkpoint — "
        "this is why resume recovers nothing for long-running agents"
    )


def test_the_step_trigger_still_works():
    """The existing behaviour must be preserved exactly."""
    clock = FakeClock()
    cp = CheckpointManager(run_id="r1", clock=clock)
    for _ in range(CHECKPOINT_INTERVAL - 1):
        cp.record_success()
    assert not cp.should_checkpoint()
    cp.record_success()
    assert cp.should_checkpoint(), "the 5-tool-call trigger regressed"


def test_it_does_not_checkpoint_before_any_work():
    """A run that has done nothing has nothing worth persisting."""
    clock = FakeClock()
    cp = CheckpointManager(run_id="r1", clock=clock)
    clock.advance(3600)
    assert not cp.should_checkpoint()


def test_the_clock_resets_after_a_save():
    """Otherwise every subsequent iteration checkpoints forever."""
    clock = FakeClock()
    cp = CheckpointManager(run_id="r1", clock=clock)
    cp.record_success()
    clock.advance(3600)
    assert cp.should_checkpoint()

    cp.note_checkpoint_saved()
    assert not cp.should_checkpoint(), "the time trigger never rearmed"
    clock.advance(3600)
    assert cp.should_checkpoint()
