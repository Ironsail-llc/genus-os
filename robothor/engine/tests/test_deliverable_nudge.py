"""Telling an agent it hasn't produced the artifact, while it can still act.

The deliverable contract detects a missing artifact in `run_finalizer` — AFTER
the loop has ended. At that point it can only report. The run this exists for
(WildClawBench task_4) spent 333 requests and 704 seconds, said "completed",
and wrote nothing: a verdict written after the fact turns a 0.0 into a
*documented* 0.0.

The loop already has the mechanism. When an agent stops calling tools in plan
mode, runner.py appends an ENGINE_CONTEXT_ROLE message and `continue`s instead
of returning. A named-but-absent deliverable deserves the same: say so while
there are still iterations left, and let the agent write the file.

Bounded on purpose. One nudge, then the run ends whatever the agent does —
an unbounded "you're not done" is a loop, and this repo has paid for those.
"""

from __future__ import annotations

from pathlib import Path

from robothor.engine.deliverable_contract import deliverable_nudge


class _Session:
    def __init__(self, message: str):
        self.originating_message = message


class TestItSpeaksOnlyWhenItShould:
    def test_a_named_but_absent_deliverable_produces_a_nudge(self):
        s = _Session("Do the thing and save it to:\n\n- `/tmp/definitely_not_here.tsv`\n")
        nudge = deliverable_nudge(s, nudges_used=0)
        assert nudge and "/tmp/definitely_not_here.tsv" in nudge

    def test_a_task_naming_no_deliverable_is_silent(self):
        """Most runs. A nudge here would be noise on every single one."""
        assert deliverable_nudge(_Session("Summarise the inbox"), nudges_used=0) is None

    def test_a_satisfied_deliverable_is_silent(self, tmp_path: Path):
        target = tmp_path / "out.tsv"
        target.write_text("done")
        s = _Session(f"Save the table to {target}")
        assert deliverable_nudge(s, nudges_used=0) is None

    def test_no_session_is_silent(self):
        assert deliverable_nudge(None, nudges_used=0) is None


class TestItCannotLoop:
    def test_it_fires_at_most_once(self):
        s = _Session("Save it to:\n\n- `/tmp/definitely_not_here.tsv`\n")
        assert deliverable_nudge(s, nudges_used=0) is not None
        assert deliverable_nudge(s, nudges_used=1) is None, (
            "an unbounded 'you are not done' is a loop, not a nudge"
        )


class TestTheLoopActuallyUsesIt:
    """A nudge nothing calls is the failure mode this whole session has been
    about."""

    def test_the_runner_consults_it_before_returning(self):
        src = (Path(__file__).resolve().parents[1] / "runner.py").read_text()
        assert "nudge_for_missing_deliverable" in src, "the run loop never asks about the deliverable"
        # It must be consulted on the no-tool-calls path, which is where the
        # agent declares itself finished.
        branch = src.split("if not assistant_msg.tool_calls:", 1)[-1].split("# ── Execute", 1)[0]
        assert "nudge_for_missing_deliverable" in branch, (
            "the check is not on the path where the agent says it is done"
        )


class TestTheGuardItselfRuns:
    """The tests above exercised the pure helper and the source wiring, and
    both passed while `nudge_for_missing_deliverable` raised ImportError on its
    first line — it imported ENGINE_CONTEXT_ROLE from `prompts`, which does not
    define it. A probe found that, not the suite.

    So: actually call it.
    """

    def test_it_appends_a_message_and_reports_that_it_did(self):
        from robothor.engine.loop_guards import nudge_for_missing_deliverable

        class _S:
            originating_message = "Save it to:\n\n- `/tmp/nudge_probe_absent.tsv`\n"
            messages: list = []

        s = _S()
        s.messages = []
        assert nudge_for_missing_deliverable(s) is True
        assert len(s.messages) == 1
        assert "/tmp/nudge_probe_absent.tsv" in s.messages[0]["content"]

    def test_the_appended_role_is_one_the_llm_client_recognises(self):
        from robothor.engine.loop_guards import nudge_for_missing_deliverable
        from robothor.engine.session import ENGINE_CONTEXT_ROLE

        class _S:
            originating_message = "Save it to:\n\n- `/tmp/nudge_probe_absent.tsv`\n"
            messages: list = []

        s = _S()
        s.messages = []
        nudge_for_missing_deliverable(s)
        assert s.messages[0]["role"] == ENGINE_CONTEXT_ROLE

    def test_the_budget_is_spent_after_one(self):
        from robothor.engine.loop_guards import nudge_for_missing_deliverable

        class _S:
            originating_message = "Save it to:\n\n- `/tmp/nudge_probe_absent.tsv`\n"
            messages: list = []

        s = _S()
        s.messages = []
        assert nudge_for_missing_deliverable(s) is True
        assert nudge_for_missing_deliverable(s) is False
        assert len(s.messages) == 1

    def test_a_task_with_no_deliverable_appends_nothing(self):
        from robothor.engine.loop_guards import nudge_for_missing_deliverable

        class _S:
            originating_message = "Summarise the inbox"
            messages: list = []

        s = _S()
        s.messages = []
        assert nudge_for_missing_deliverable(s) is False
        assert s.messages == []
