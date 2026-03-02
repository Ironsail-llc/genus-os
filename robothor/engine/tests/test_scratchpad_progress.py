"""Tests for plan-aware progress tracking in scratchpad."""

from __future__ import annotations

from robothor.engine.scratchpad import Scratchpad


class TestScratchpadPlanTracking:
    def _make_plan(self):
        return [
            {"step": 1, "action": "Read inbox", "tool": "read_file"},
            {"step": 2, "action": "Classify emails", "tool": "exec"},
            {"step": 3, "action": "Create tasks", "tool": "create_task"},
            {"step": 4, "action": "Write summary", "tool": "write_file"},
        ]

    def test_set_plan(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        assert sp.total_plan_steps == 4
        assert sp.steps_completed == 0
        assert sp._current_step == 0

    def test_step_matching_advances(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("read_file")  # matches step 1
        assert sp.steps_completed == 1
        assert sp._current_step == 1

    def test_step_matching_sequential(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("read_file")  # step 1
        sp.record_tool_call("exec")  # step 2
        sp.record_tool_call("create_task")  # step 3
        assert sp.steps_completed == 3
        assert sp._current_step == 3

    def test_all_steps_completed(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("read_file")
        sp.record_tool_call("exec")
        sp.record_tool_call("create_task")
        sp.record_tool_call("write_file")
        assert sp.steps_completed == 4
        assert sp._current_step == 4

    def test_non_matching_tool_no_advance(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("web_fetch")  # doesn't match step 1 (read_file)
        assert sp.steps_completed == 0
        assert sp._current_step == 0

    def test_no_tool_step_advances_on_any_success(self):
        plan = [
            {"step": 1, "action": "Think about it"},  # no tool
            {"step": 2, "action": "Read file", "tool": "read_file"},
        ]
        sp = Scratchpad()
        sp.set_plan(plan)

        sp.record_tool_call("exec")  # any success advances no-tool step
        assert sp.steps_completed == 1
        assert sp._current_step == 1

    def test_error_tracks_step_attempts(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("read_file", error="file not found")
        assert sp.current_step_attempts == 1
        assert sp.steps_completed == 0

    def test_multiple_errors_on_same_step(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        sp.record_tool_call("read_file", error="not found")
        sp.record_tool_call("read_file", error="not found again")
        sp.record_tool_call("read_file", error="still not found")
        assert sp.current_step_attempts == 3

    def test_format_summary_with_plan_on_track(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        sp.record_tool_call("read_file")
        sp.record_tool_call("exec")
        sp.record_tool_call("create_task")

        summary = sp.format_summary()
        assert "Step 4/4" in summary
        assert "on track" in summary

    def test_format_summary_stuck(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")
        sp.record_tool_call("read_file", error="fail")

        summary = sp.format_summary()
        assert "Stuck" in summary
        assert "3 failed attempts" in summary

    def test_format_summary_all_complete(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        for tool in ["read_file", "exec", "create_task", "write_file"]:
            sp.record_tool_call(tool)

        summary = sp.format_summary()
        assert "4/4 complete" in summary

    def test_format_summary_legacy_fallback(self):
        """Without plan, falls back to percentage-based progress."""
        sp = Scratchpad()
        sp.record_tool_call("t1")
        sp.record_tool_call("t2")
        sp.record_tool_call("t3")

        summary = sp.format_summary(plan_steps=6)
        assert "50%" in summary

    def test_out_of_order_execution(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())

        # Execute step 3's tool before step 1's
        sp.record_tool_call("create_task")  # matches step 3
        assert sp.steps_completed == 1
        assert 2 in sp._completed_steps  # step index 2

    def test_to_dict_preserves_plan_state(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        sp.record_tool_call("read_file")
        sp.record_tool_call("exec", error="fail")

        data = sp.to_dict()
        restored = Scratchpad.from_dict(data)
        assert restored.total_plan_steps == 4
        assert restored.steps_completed == 1
        assert restored._current_step == 1
        assert restored._step_attempts.get(1, 0) == 1

    def test_set_plan_resets_state(self):
        sp = Scratchpad()
        sp.set_plan(self._make_plan())
        sp.record_tool_call("read_file")

        new_plan = [{"step": 1, "action": "New", "tool": "exec"}]
        sp.set_plan(new_plan)
        assert sp.total_plan_steps == 1
        assert sp.steps_completed == 0
        assert sp._current_step == 0


class TestScratchpadExistingBehavior:
    """Ensure plan tracking doesn't break existing scratchpad behavior."""

    def test_record_success(self):
        sp = Scratchpad()
        sp.record_tool_call("read_file")
        assert sp._tool_calls == 1
        assert sp._successes == 1

    def test_record_error(self):
        sp = Scratchpad()
        sp.record_tool_call("exec", error="Command failed")
        assert sp._errors == 1

    def test_should_inject_at_interval(self):
        sp = Scratchpad(inject_interval=3)
        sp.record_tool_call("t1")
        sp.record_tool_call("t2")
        assert not sp.should_inject()
        sp.record_tool_call("t3")
        assert sp.should_inject()

    def test_max_injections(self):
        sp = Scratchpad(inject_interval=1, max_injections=2)
        sp.record_tool_call("t1")
        assert sp.should_inject()
        sp.format_summary()
        sp.record_tool_call("t2")
        assert sp.should_inject()
        sp.format_summary()
        sp.record_tool_call("t3")
        assert not sp.should_inject()

    def test_basic_to_dict_from_dict(self):
        sp = Scratchpad()
        sp.record_tool_call("a")
        sp.record_tool_call("b", error="oops")

        data = sp.to_dict()
        restored = Scratchpad.from_dict(data)
        assert restored._tool_calls == 2
        assert restored._successes == 1
        assert restored._errors == 1
