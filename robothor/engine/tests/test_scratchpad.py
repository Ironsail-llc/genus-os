"""Tests for the scratchpad working memory."""

from __future__ import annotations

from robothor.engine.scratchpad import Scratchpad


class TestScratchpad:
    def test_record_success(self):
        sp = Scratchpad()
        sp.record_tool_call("read_file")
        assert sp._tool_calls == 1
        assert sp._successes == 1
        assert sp._errors == 0

    def test_record_error(self):
        sp = Scratchpad()
        sp.record_tool_call("exec", error="Command failed")
        assert sp._tool_calls == 1
        assert sp._errors == 1
        assert sp._successes == 0

    def test_should_inject_at_interval(self):
        sp = Scratchpad(inject_interval=3)
        assert not sp.should_inject()

        sp.record_tool_call("tool1")
        assert not sp.should_inject()
        sp.record_tool_call("tool2")
        assert not sp.should_inject()
        sp.record_tool_call("tool3")
        assert sp.should_inject()

    def test_should_inject_at_zero(self):
        sp = Scratchpad()
        assert not sp.should_inject()

    def test_format_summary(self):
        sp = Scratchpad()
        sp.record_tool_call("read_file")
        sp.record_tool_call("exec", error="failed")
        sp.record_tool_call("create_task")

        summary = sp.format_summary()
        assert "[WORKING STATE]" in summary
        assert "3" in summary  # tool calls
        assert "2 ok" in summary
        assert "1 errors" in summary
        assert "read_file" in summary

    def test_format_summary_with_plan_progress(self):
        sp = Scratchpad()
        for _ in range(3):
            sp.record_tool_call("tool")
        summary = sp.format_summary(plan_steps=6)
        assert "50%" in summary

    def test_to_dict_and_from_dict(self):
        sp = Scratchpad(inject_interval=5)
        sp.record_tool_call("a")
        sp.record_tool_call("b", error="oops")

        data = sp.to_dict()
        restored = Scratchpad.from_dict(data, inject_interval=5)
        assert restored._tool_calls == 2
        assert restored._successes == 1
        assert restored._errors == 1
        assert restored._recent_actions == ["a", "b"]

    def test_recent_actions_capped(self):
        sp = Scratchpad()
        for i in range(15):
            sp.record_tool_call(f"tool_{i}")
        assert len(sp._recent_actions) == 10

    def test_max_injections_stops_injecting(self):
        """After max_injections, should_inject returns False."""
        sp = Scratchpad(inject_interval=1, max_injections=2)

        sp.record_tool_call("t1")
        assert sp.should_inject()
        sp.format_summary()  # injection 1

        sp.record_tool_call("t2")
        assert sp.should_inject()
        sp.format_summary()  # injection 2

        sp.record_tool_call("t3")
        assert not sp.should_inject()  # limit reached

    def test_tracking_continues_after_max_injections(self):
        """Internal tracking continues even after injection stops."""
        sp = Scratchpad(inject_interval=1, max_injections=1)

        sp.record_tool_call("t1")
        sp.format_summary()  # injection 1

        sp.record_tool_call("t2")
        sp.record_tool_call("t3")
        # Tracking continues
        assert sp._tool_calls == 3
        assert sp._successes == 3

    def test_default_max_injections_is_five(self):
        sp = Scratchpad()
        assert sp.max_injections == 5

    def test_format_summary_increments_count(self):
        sp = Scratchpad()
        for _ in range(3):
            sp.record_tool_call("t")
        sp.format_summary()
        assert sp._injected_count == 1
