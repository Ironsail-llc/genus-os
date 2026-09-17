"""Asking twice and being ignored twice is not a control.

The deliverable check-in asks "what have you WRITTEN". Measured across the
2026-09-16 and 09-17 sweeps, the runs that died at their budget answered it
with a plan — and kept exploring. On `arxiv_digest` the check-in fired at
iteration 50, the agent described what it was going to write, and the run
ended at its budget with `results/` created and empty.

So the third time, past halfway through the budget, it stops being a question.
The engine names the path and says that nothing but writing it comes next; the
shape conformance check from the deliverable contract is appended to it exactly
as it is to the ordinary check-in, so the file it asks for is checked against
the task rather than against the agent's own plan.

Deliberately evidence-driven rather than answer-driven: the trigger is the
workspace, not the model's prose. An agent that SAYS it has written the file
and has not is the exact failure being caught, so believing the prose would be
believing the thing under test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _session() -> SimpleNamespace:
    return SimpleNamespace(run_id="r1")


def _note(session, tmp_path, fraction: float, wanted: str, iteration: int = 25) -> str | None:
    from robothor.engine.run_pacing import checkin_note

    return checkin_note(
        iteration,
        0,
        "enforce",
        run_id="r1",
        session=session,
        task_text=f"Write your answer to {wanted}",
        workspace=tmp_path,
        fraction=fraction,
    )


class TestTheDirectiveCheckIn:
    def test_the_first_two_are_the_ordinary_question(self, tmp_path: Path) -> None:
        session = _session()
        wanted = str(tmp_path / "results.md")
        first = _note(session, tmp_path, 0.6, wanted, iteration=25)
        assert first is not None and "what have you WRITTEN" in first
        assert "STOP" not in first

    def test_the_second_one_past_halfway_becomes_directive(self, tmp_path: Path) -> None:
        session = _session()
        wanted = str(tmp_path / "results.md")
        _note(session, tmp_path, 0.6, wanted, iteration=25)
        second = _note(session, tmp_path, 0.7, wanted, iteration=50)
        assert second is not None
        assert wanted in second
        assert "STOP" in second

    def test_nothing_becomes_directive_before_halfway(self, tmp_path: Path) -> None:
        """Early in a run, "nothing written yet" is what working looks like."""
        session = _session()
        wanted = str(tmp_path / "results.md")
        for iteration in (25, 50, 75):
            note = _note(session, tmp_path, 0.2, wanted, iteration=iteration)
            assert note is not None and "STOP" not in note

    def test_a_run_that_wrote_the_file_is_never_made_to_stop(self, tmp_path: Path) -> None:
        session = _session()
        wanted = tmp_path / "results.md"
        _note(session, tmp_path, 0.6, str(wanted), iteration=25)
        wanted.write_text("a partial answer", encoding="utf-8")
        second = _note(session, tmp_path, 0.7, str(wanted), iteration=50)
        assert second is not None and "STOP" not in second

    def test_writing_the_file_resets_the_count(self, tmp_path: Path) -> None:
        """A run that writes, then explores again, starts the ladder over."""
        session = _session()
        wanted = tmp_path / "results.md"
        _note(session, tmp_path, 0.6, str(wanted), iteration=25)
        wanted.write_text("a partial answer", encoding="utf-8")
        _note(session, tmp_path, 0.65, str(wanted), iteration=50)
        wanted.unlink()
        third = _note(session, tmp_path, 0.7, str(wanted), iteration=75)
        assert third is not None and "STOP" not in third

    def test_a_task_that_named_no_path_is_never_made_directive(self, tmp_path: Path) -> None:
        """With nothing declared, "nothing written" is not a fact we have."""
        from robothor.engine.run_pacing import checkin_note

        session = _session()
        for iteration in (25, 50, 75, 100):
            note = checkin_note(
                iteration,
                0,
                "enforce",
                run_id="r1",
                session=session,
                task_text="Summarise the paper and tell me what you think.",
                workspace=tmp_path,
                fraction=0.9,
            )
            assert note is not None and "STOP" not in note

    def test_off_never_escalates(self, tmp_path: Path) -> None:
        from robothor.engine.run_pacing import checkin_note

        session = _session()
        wanted = str(tmp_path / "results.md")
        for iteration in (25, 50, 75):
            note = checkin_note(
                iteration,
                0,
                "off",
                run_id="r1",
                session=session,
                task_text=f"Write your answer to {wanted}",
                workspace=tmp_path,
                fraction=0.9,
            )
            assert note is None or "STOP" not in note

    def test_the_shipped_call_signature_still_works(self) -> None:
        """Every existing caller passes three positional arguments and a run id."""
        from robothor.engine.run_pacing import checkin_note

        assert checkin_note(25, 25, "off", run_id="r1") is not None


class TestTheEvidenceIsTheWorkspace:
    @pytest.mark.parametrize("claim", ["I have written the file.", "Done — saved to disk."])
    def test_a_claim_that_the_file_exists_does_not_count(self, tmp_path: Path, claim: str) -> None:
        """The prose is the thing under test; only the workspace is evidence."""
        session = _session()
        session.messages = [{"role": "assistant", "content": claim}]
        wanted = str(tmp_path / "results.md")
        _note(session, tmp_path, 0.6, wanted, iteration=25)
        second = _note(session, tmp_path, 0.7, wanted, iteration=50)
        assert second is not None and "STOP" in second
