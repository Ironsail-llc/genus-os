"""A successful call that changes nothing is not progress.

Traced through one benchmark run: 29 consecutive `sleep 25; cat api2_out.txt`
calls, exit 0 every time, byte-identical stdout, the file's mtime unchanged
throughout — 65% of the run's wall-clock budget. Nothing in the engine saw
it, and two mechanisms actively misread it as health:

* `EscalationManager` keys on ERRORS. A successful useless call is invisible.
* `Scratchpad._try_advance_step` advances the plan on any successful call
  matching the current step's tool, so `steps_completed` RISES during the
  loop, and `should_replan` then sees healthy progress.

So a polling loop reads as steady progress to every progress-sensing
component at once. This adds the missing signal: identical (tool, args,
result) repeated with nothing changing is a stall, and the agent is told so
by name.

Polling is legitimate work — waiting on a build, a deploy, a queue. The
detector fires on repetition WITHOUT CHANGE, not on repetition.
"""

from __future__ import annotations

from robothor.engine.scratchpad import LOOP_REPEAT_THRESHOLD, Scratchpad


class TestDetectingTheStall:
    def _spin(self, sp, n, result="same bytes"):
        for _ in range(n):
            sp.record_tool_call("exec", result=result, tool_input={"command": "cat out.txt"})

    def test_identical_calls_trip_the_detector(self):
        sp = Scratchpad()
        self._spin(sp, LOOP_REPEAT_THRESHOLD)
        assert sp.stalled_signature is not None

    def test_below_the_threshold_is_not_a_stall(self):
        sp = Scratchpad()
        self._spin(sp, LOOP_REPEAT_THRESHOLD - 1)
        assert sp.stalled_signature is None

    def test_a_changed_result_is_progress(self):
        """Polling a build that is actually producing output must not trip."""
        sp = Scratchpad()
        for i in range(LOOP_REPEAT_THRESHOLD + 3):
            sp.record_tool_call("exec", result=f"line {i}", tool_input={"command": "cat out.txt"})
        assert sp.stalled_signature is None

    def test_different_arguments_are_progress(self):
        sp = Scratchpad()
        for i in range(LOOP_REPEAT_THRESHOLD + 3):
            sp.record_tool_call("exec", result="same", tool_input={"command": f"cat {i}.txt"})
        assert sp.stalled_signature is None

    def test_a_different_tool_breaks_the_streak(self):
        sp = Scratchpad()
        self._spin(sp, LOOP_REPEAT_THRESHOLD - 1)
        sp.record_tool_call("write_file", result="ok", tool_input={"path": "a"})
        self._spin(sp, 1)
        assert sp.stalled_signature is None

    def test_an_error_is_not_this_signal(self):
        """Errors already have their own escalation path."""
        sp = Scratchpad()
        for _ in range(LOOP_REPEAT_THRESHOLD + 2):
            sp.record_tool_call("exec", error="boom", tool_input={"command": "x"})
        assert sp.stalled_signature is None

    def test_recovery_clears_it(self):
        sp = Scratchpad()
        self._spin(sp, LOOP_REPEAT_THRESHOLD)
        assert sp.stalled_signature is not None
        sp.record_tool_call("exec", result="something new", tool_input={"command": "cat out.txt"})
        assert sp.stalled_signature is None

    def test_unhashable_and_missing_input_do_not_raise(self):
        sp = Scratchpad()
        sp.record_tool_call("exec", result="x")
        sp.record_tool_call("exec", result="x", tool_input={"o": object()})
        sp.record_tool_call("exec", result=None, tool_input=None)


class TestTellingTheAgent:
    def test_the_summary_names_the_repeated_call(self):
        sp = Scratchpad()
        for _ in range(LOOP_REPEAT_THRESHOLD):
            sp.record_tool_call("exec", result="frozen", tool_input={"command": "cat out.txt"})
        summary = sp.format_summary()
        assert "no new information" in summary.lower()
        assert "exec" in summary
        assert str(LOOP_REPEAT_THRESHOLD) in summary

    def test_a_healthy_run_says_nothing_about_loops(self):
        sp = Scratchpad()
        for i in range(6):
            sp.record_tool_call("exec", result=f"out {i}", tool_input={"command": f"c{i}"})
        assert "no new information" not in sp.format_summary().lower()


class TestPlanProgressIsNotFakedByALoop:
    def test_a_stalled_repeat_does_not_advance_the_plan(self):
        """`_try_advance_step` advanced on ANY matching successful call, so a
        polling loop walked the plan forward while achieving nothing."""
        sp = Scratchpad()
        sp.set_plan(
            [
                {"tool": "exec", "action": "poll"},
                {"tool": "exec", "action": "next"},
                {"tool": "exec", "action": "later"},
            ]
        )
        for _ in range(LOOP_REPEAT_THRESHOLD + 4):
            sp.record_tool_call("exec", result="frozen", tool_input={"command": "cat out.txt"})
        # The first repeats sit below the threshold and legitimately advance;
        # what must never happen is a frozen loop walking the plan to
        # COMPLETION, which is what made `should_replan` see healthy progress.
        assert sp.steps_completed < sp.total_plan_steps, (
            f"a no-progress loop completed the whole plan ({sp.steps_completed})"
        )
        before = sp.steps_completed
        for _ in range(10):
            sp.record_tool_call("exec", result="frozen", tool_input={"command": "cat out.txt"})
        assert sp.steps_completed == before, "the plan kept advancing while stalled"

    def test_real_work_still_advances_the_plan(self):
        sp = Scratchpad()
        sp.set_plan([{"tool": "exec", "action": "a"}, {"tool": "exec", "action": "b"}])
        sp.record_tool_call("exec", result="first", tool_input={"command": "one"})
        sp.record_tool_call("exec", result="second", tool_input={"command": "two"})
        assert sp.steps_completed == 2


class TestTheRunnerFeedsTheDetector:
    """A detector the runner never feeds is decoration.

    `scratchpad.record_tool_call(tool_name, error=error_msg)` passed neither
    the result nor the arguments, so the signature could never differ and the
    stall could never fire. This is the inert-caller shape that has produced
    most of this codebase's silent controls.
    """

    def test_the_runner_passes_result_and_input(self):
        from pathlib import Path

        import robothor.engine.runner as m

        source = Path(m.__file__).read_text(encoding="utf-8")
        idx = source.index("scratchpad.record_tool_call(")
        call = source[idx : idx + 260]
        assert "result=" in call, "the runner never passes the result — detector is inert"
        assert "tool_input=" in call, "the runner never passes the arguments"
