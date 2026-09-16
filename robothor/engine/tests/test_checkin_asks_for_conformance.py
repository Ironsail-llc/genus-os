"""The deliverable check-in has to ask whether the file is RIGHT, not whether it exists.

MEASURED 2026-09-16. The path-only check-in fired once on a task whose spec
named six columns in an exact order. The agent complied — with the path. It
wrote a five-column file of its own naming to exactly the right place, every
grader criterion scored 0, and from the moment that file existed every pacing
control in the engine fell silent. A check-in that stops at "is it there"
certifies the failure it was built to catch.

Two halves. The wording asks for a quote-and-compare against the task's own
format, which works on any task. And where the engine has already extracted the
contract, the note carries the comparison itself — the agent does not have to
be trusted to do the diff it just failed to do.
"""

from __future__ import annotations

from robothor.engine.run_pacing import CHECKIN_EVERY, checkin_note


def _deliverable_note() -> str:
    note = checkin_note(CHECKIN_EVERY, checkin_interval=0, mode="enforce", run_id="r")
    assert note is not None
    return note


class TestTheWording:
    def test_it_still_asks_where(self):
        assert "WRITTEN to the deliverable path" in _deliverable_note()

    def test_it_asks_the_agent_to_quote_the_required_format(self):
        """Quoting is the part that cannot be done from memory. An agent that
        can still quote the spec has not lost it to compaction."""
        note = _deliverable_note()
        assert "quote the task's required format" in note.lower()

    def test_it_names_the_shapes_that_scored_zero(self):
        note = _deliverable_note()
        for shape in ("filename", "header", "field names", "section headings"):
            assert shape in note, shape

    def test_it_says_a_wrong_shape_is_worth_nothing(self):
        """The old note's only quantitative claim was that a partial file beats
        no file. True, and it is what produced a complete file that scored 0."""
        assert "wrong shape scores the same as no file" in _deliverable_note()

    def test_it_compares_against_the_task_not_the_plan(self):
        """The measured runs all self-verified: one asserted its own chosen
        field names were present and called that validation."""
        assert "not against your own plan" in _deliverable_note()

    def test_the_shipped_checkin_is_unchanged(self):
        """The cadence that already shipped stays exactly as it was; this
        control only adds."""
        shipped = checkin_note(10, checkin_interval=10, mode="off", run_id="r")
        assert shipped is not None
        assert "Are you making progress" in shipped


class TestTheComparisonRidesAlong:
    """Where the contract is known, the note carries the diff already done."""

    def test_the_check_in_carries_the_comparison(self, tmp_path, monkeypatch):
        from robothor.engine.loop_guards import append_engine_note

        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: "enforce"
        )
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "rows.tsv").write_text("Title\tSpeakers\n", encoding="utf-8")

        class _Session:
            originating_message = (
                "Save the table to `/work/results/rows.tsv`.\n\n"
                "The TSV must use exactly the following header:\n\n"
                "```text\nTrack\tTitle\tSpeakers\n```\n"
            )

        session = _Session()
        session.messages = []
        append_engine_note(session, _deliverable_note(), str(tmp_path))
        content = session.messages[0]["content"]
        assert "quote the task's required format" in content.lower()
        assert "Track Title Speakers" in content, "the comparison did not ride along"
