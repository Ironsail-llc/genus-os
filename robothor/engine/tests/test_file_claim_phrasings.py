"""The file-write claim detector was blind to the way agents actually write.

`_FILE_PATTERNS` required either the literal word "file" or an explicit path
after "wrote to"/"saved to". So an agent that says

    The report is saved.

makes no claim at all as far as the control is concerned — passive voice,
and the noun is the artefact's name rather than "file".

That sentence is verbatim from WildClawBench
`03_Social_Interaction_task_3_chat_multi_step_reasoning`, 2026-08-25. The
agent had written to `tmp_workspace/results/results.md`, a relative path that
resolved to `/tmp_workspace/tmp_workspace/results/results.md`, so the grader
found nothing at the expected location and the task scored zero. The claim
was false and the control that exists to catch false claims never saw one.

This is the same failure `run_verification` was written to fix, one level on:
its own docstring says the older control was "structurally blind" because a
real phrasing matched none of its regexes. A claim detector is only as good
as its coverage of how the thing is actually said.
"""

from __future__ import annotations

from robothor.engine.run_verification import extract_claims


def _kinds(text: str) -> set[str]:
    return {c.kind for c in extract_claims(text)}


class TestTheIncidentPhrasing:
    def test_the_report_is_saved(self):
        assert "file_written" in _kinds("The report is saved. Here's the summary:")

    def test_the_full_incident_output(self):
        text = (
            "The report is saved. Here's the executive summary of what I found:\n\n"
            "---\n\n## The TL;DR for VP Sales\n"
        )
        assert "file_written" in _kinds(text)


class TestPassiveVoice:
    def test_has_been_written(self):
        assert "file_written" in _kinds("The summary has been written.")

    def test_was_created(self):
        assert "file_written" in _kinds("results.md was created with the findings.")

    def test_named_artefacts(self):
        for noun in ("report", "summary", "digest", "manifest", "output"):
            assert "file_written" in _kinds(f"The {noun} is saved."), noun


class TestActiveVoiceStillWorks:
    def test_the_original_phrasings_are_unaffected(self):
        assert "file_written" in _kinds("I wrote the file to disk.")
        assert "file_written" in _kinds("Saved it to ./results/out.md")


class TestItDoesNotOverreach:
    def test_a_question_is_not_a_claim(self):
        assert "file_written" not in _kinds("Should the report be saved?")

    def test_a_negated_claim_is_not_a_claim(self):
        """Negation handling already exists and must keep applying."""
        assert "file_written" not in _kinds("The report is not saved.")

    def test_an_intention_is_not_a_claim(self):
        assert "file_written" not in _kinds("I will make sure the report is saved.")

    def test_quoted_text_is_not_a_claim(self):
        """A claim the agent is REPORTING is not one it is MAKING — the
        masking that already exists has to keep covering this."""
        assert "file_written" not in _kinds('The user asked: "is the report saved?"')

    def test_unrelated_prose_is_untouched(self):
        assert "file_written" not in _kinds("The meeting is scheduled for Tuesday.")
