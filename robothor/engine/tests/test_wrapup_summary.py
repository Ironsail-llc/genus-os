"""What the operator is told when a run is cut short.

`_synthesize_wrapup_summary` is the fallback final summary: when a run hits
its iteration cap or deadline and the wrap-up LLM call produces no text,
this is what the operator reads. Its whole purpose is that "a truncated run
that did real work is not reported as empty output" — and it had no test.

The failure it guards against is one this instance has seen repeatedly
tonight: a run that did the work and reported nothing, which is
indistinguishable from a run that did nothing. Silence reads as failure, and
an operator who cannot tell the difference stops trusting the channel.
"""

from __future__ import annotations

from robothor.engine.models import StepType
from robothor.engine.run_lifecycle import RunLifecycleMixin
from robothor.engine.session import AgentSession

_synth = RunLifecycleMixin._synthesize_wrapup_summary


def _session_with(tools: list[str]) -> AgentSession:
    session = AgentSession("test-agent")
    session.start("sys", "user", [])
    for i, name in enumerate(tools):
        session.record_tool_call(name, {}, "{}", f"call-{i}")
    return session


class TestItNamesTheWorkThatWasDone:
    def test_completed_tools_are_listed(self):
        out = _synth(_session_with(["search_memory", "create_task"]), "iteration cap")
        assert "search_memory" in out and "create_task" in out
        assert "2 tool action(s)" in out

    def test_the_reason_is_stated(self):
        """An operator needs to know it was cut short, not that it finished."""
        assert "iteration cap" in _synth(_session_with(["exec"]), "iteration cap")

    def test_repeats_are_collapsed(self):
        """Twenty search_memory calls is one kind of work, not twenty."""
        out = _synth(_session_with(["search_memory"] * 20), "deadline")
        assert "1 tool action(s)" in out
        assert out.count("search_memory") == 1

    def test_order_of_first_use_is_preserved(self):
        """Substring position would match letters inside the prose — parse the list."""
        out = _synth(_session_with(["beta", "alpha", "beta", "gamma"]), "cap")
        listed = out.rsplit(": ", 1)[1].rstrip(".").split(", ")
        assert listed == ["beta", "alpha", "gamma"]


class TestARunThatDidNothing:
    def test_says_so_plainly(self):
        out = _synth(_session_with([]), "deadline")
        assert "No output was produced" in out
        assert "deadline" in out

    def test_llm_only_steps_do_not_count_as_work(self):
        """Thinking is not doing; an LLM call is not a tool action."""
        session = AgentSession("test-agent")
        session.start("sys", "user", [])
        session.record_llm_call(model="m", assistant_message={"role": "assistant", "content": "hm"})
        assert "No output was produced" in _synth(session, "cap")


class TestItNeverProducesEmptyOutput:
    def test_a_step_with_no_tool_name_is_skipped_not_crashed(self):
        session = _session_with(["real_tool"])
        session.run.steps.append(
            type(session.run.steps[0])(
                run_id=session.run.id, step_number=99, step_type=StepType.TOOL_CALL, tool_name=None
            )
        )
        out = _synth(session, "cap")
        assert "real_tool" in out and "1 tool action(s)" in out

    def test_the_result_is_always_non_empty(self):
        """Its entire job is to replace an empty final output."""
        for tools in ([], ["a"], ["a", "b", "c"]):
            assert _synth(_session_with(tools), "cap").strip()
