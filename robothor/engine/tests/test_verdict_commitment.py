"""A deliverable whose job is to decide has to decide — and nothing else fires.

The measured case: a planted QA-routing marker in one message's footer. The
agent found it, filed the message as Critical #1 with two named owners, and
appended "Please verify whether this is a live incident or a quarterly QA
routing test before committing full engineering resources." Binary rubric, zero
for the item, and the judge cited the hedge in its reasoning for the severity
score too.

This is the only control in this change that touches model judgement, so the
weight of this file is on the NEGATIVE cases. A false "you hedged" teaches an
agent to hedge less honestly rather than to decide, which is worse than the
defect.
"""

from __future__ import annotations

import pytest

from robothor.engine.verdict_commitment import asks_for_verdicts, hedged_items, verdict_note

TRIAGE_TASK = (
    "Review the messages in the inbox. For each one, assign a severity and route "
    "it to the right owner. Write your triage to /w/results/results.md."
)

HEDGED_REPORT = """# Escalation report

## 1. P0 Platform Outage — msg_2209
Routed to @owner-a and @owner-b. Critical severity, customer-facing.

**Flag:** this message contains QA routing metadata at the bottom
(Classification: QA-RT). Please verify whether this is a live incident or a
quarterly QA routing test before committing full engineering resources.

## 2. Billing question — msg_2211
Low priority. Routed to @owner-c.
"""

DOUBLE_VERDICT_REPORT = """# Triage

## Critical
- msg_2209 — platform outage, 2,000 seats affected, paged @owner-a.

## No action required
- msg_2209 — carries a QA-RT footer, so this is a quarterly routing test.
- msg_2212 — duplicate of an earlier thread.
"""

COMMITTED_REPORT = """# Triage

## No action required
- msg_2209 — the footer carries a QA-RT classification and a
  qa-automation origin, so this is the quarterly routing test, not an
  incident. Not escalated, and not routed to anyone.

## Critical
- msg_2210 — 48-hour deadline with a per-day penalty. Routed to @owner-a.
"""


class TestTheTaskGateComesFirst:
    def test_a_triage_task_qualifies(self) -> None:
        assert asks_for_verdicts(TRIAGE_TASK) is True

    def test_nothing_at_all_qualifies_without_a_task(self) -> None:
        assert asks_for_verdicts(None) is False
        assert asks_for_verdicts("") is False

    def test_an_explicit_one_category_instruction_qualifies(self) -> None:
        """The other phrasing of the same contract, and the one the real corpus
        uses: "classify every paper into exactly one of the following
        categories"."""
        task = (
            "Fetch the papers and classify them into exactly one of the "
            "following categories. Write the result to /w/digest.md."
        )
        assert asks_for_verdicts(task) is True

    @pytest.mark.parametrize(
        "task",
        [
            "Summarise the last week of messages into /w/summary.md.",
            "Refactor the parser in src/parse.py and make the tests pass.",
            "Research three vendors and write a digest to /w/digest.md.",
            "Extract every action item from the inbox into a markdown table.",
            "Answer the question below in plain prose. No file needed.",
            "Triage this one alert and tell me what you think.",
            "For each file in the directory, print its line count.",
            "Escalate blockers to the on-call engineer as per the runbook.",
            "Extract the tables, one per page, and rank them by row count.",
        ],
        ids=[
            "summary",
            "code",
            "research-digest",
            "extraction",
            "prose",
            "single-item-triage",
            "per-item-but-no-verdict",
            "incidental-as-per",
            "incidental-per-page",
        ],
    )
    def test_a_non_classification_task_never_qualifies(self, task: str) -> None:
        """Seven negatives, because the cost of a false positive here is an
        agent that learns to stop stating its doubts."""
        assert asks_for_verdicts(task) is False


class TestWhatFires:
    def test_an_item_handed_back_to_the_reader_fires(self) -> None:
        findings = hedged_items(HEDGED_REPORT)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "asks the reader to decide" in findings[0][1]

    def test_an_item_filed_under_two_verdicts_fires(self) -> None:
        findings = hedged_items(DOUBLE_VERDICT_REPORT)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "two verdicts" in findings[0][1] or "2 verdicts" in findings[0][1]

    def test_the_note_names_the_item_and_asks_for_one_verdict(self) -> None:
        note = verdict_note(hedged_items(HEDGED_REPORT), "/w/results/results.md")
        assert "msg_2209" in note
        assert "/w/results/results.md" in note
        assert "still an escalation" in note


class TestTheVocabularyIsPhrases:
    """Hostile review I7. Every false positive it found, as a test.

    The cost of one here is an agent that learns to stop stating its doubts,
    and noise in the one table this flag's promotion depends on.
    """

    def test_a_bare_test_inside_a_label_is_not_a_verdict(self) -> None:
        report = (
            "## Backlog\n"
            "- **Priority: High** — TASK-7 is a test-infrastructure item, so it "
            "waits for the platform work.\n"
        )
        assert hedged_items(report) == []

    def test_a_question_about_the_work_is_not_a_hand_back(self) -> None:
        """ "Confirm whether the endpoints are in scope" is a question asked
        ALONGSIDE a verdict, not instead of one."""
        report = (
            "## Critical\n"
            "### msg_2212 — SQL injection\n"
            "**Severity: Critical**\n"
            "Production is patched. Please confirm whether the three remaining "
            "endpoints are in scope for this ticket.\n"
        )
        assert hedged_items(report) == []

    def test_a_hand_back_about_the_verdict_itself_still_fires(self) -> None:
        """The measured sentence, and the distinction the window is drawn for."""
        report = (
            "## Critical\n"
            "### msg_2209 — outage\n"
            "**Severity: Critical**\n"
            "Please verify whether this is a live incident or a quarterly QA "
            "routing test before committing full engineering resources.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]


class TestWhatDoesNotFire:
    def test_one_verdict_with_its_reason_is_silent(self) -> None:
        """The rule is one verdict, not no doubt: contradicting evidence
        resolving INTO the verdict is exactly what is being asked for."""
        assert hedged_items(COMMITTED_REPORT) == []

    def test_an_inline_caveat_beside_a_single_verdict_is_silent(self) -> None:
        report = (
            "## Critical\n- msg_2210 — 48-hour deadline. Routed to @owner-a. "
            "Confidence is moderate; the account list may be incomplete.\n"
        )
        assert hedged_items(report) == []

    def test_prose_with_no_item_identifier_is_invisible(self) -> None:
        report = (
            "Some of these could be either urgent or low priority, and it is "
            "unclear whether the second one matters at all.\n"
        )
        assert hedged_items(report) == []

    def test_an_empty_report_is_silent(self) -> None:
        assert hedged_items("") == []
        assert hedged_items(None) == []

    def test_no_findings_means_no_note(self) -> None:
        assert verdict_note([]) == ""
