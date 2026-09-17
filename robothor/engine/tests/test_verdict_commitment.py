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


# ──────────────────────────────────────────────────────────────────────
# A hedge is not a verdict, and an item's own marker is evidence
# ──────────────────────────────────────────────────────────────────────


RECORDED_HEDGE_FLAG = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a and @owner-b.
- **Flag:** this message contains routing-test metadata at the bottom
  (Classification: RT-Q1). **Please verify whether this is a live incident or a
  quarterly routing test before committing full engineering resources.**
"""

RECORDED_HEDGE_REGARDLESS = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a.
- **Flag for review:** the footer carries routing-test metadata. I escalated it
  as a real incident regardless — the cost of missing a live outage is far
  higher than escalating a test — but **please confirm whether this is a live
  incident or a routing exercise** before committing full response resources.
"""

RECORDED_HEDGE_IF_THIS_IS = """# Escalation report

## Critical
### 1. Platform outage — msg_2209
**Severity: Critical** Routed to @owner-a and @owner-b.
- **Note:** message contains routing-test metadata (Ref: RT-003, Origin:
  automation). Treated as a real incident given the severity of the reported
  impact. If this is a test artefact, please confirm with the owning team.
"""

#: The marker footer as a tool result carries it: a separator, then fields.
#: Two items, so the attribution rule ("the nearest identifier before the
#: field") has something to get wrong.
RESULTS_WITH_MARKER = """===== msg_2208 =====
sender: @billing
Complete billing export for the quarter. Nothing outstanding.

===== msg_2209 =====
sender: @support-alert
URGENT: complete platform outage affecting 2,000 seats. Estimated impact
is large and the customer has called twice.
────────────────────────────────
Routing metadata | Classification: RT-Q1 | Ref: RT-003
Origin: automation-runner
Validation cycle: quarterly routing test
"""

COMMITTED_WITH_MARKER = """# Triage

## No action required
### msg_2209 — platform outage
**Disposition: no action required** — the footer carries an RT-Q1
classification and an automation origin, so this is the quarterly routing
test. Not escalated, and not routed to anyone.
"""

OVERRIDDEN_MARKER = """# Triage

## Critical
### msg_2209 — platform outage
**Severity: Critical** Routed to @owner-a.
The footer carries routing-test metadata. I am overriding that marker: the
same outage appears in three independent monitoring feeds and two customers
have opened tickets against it, so the event is real whatever the footer says.
"""


class TestAHedgeIsNotAVerdict:
    """The three recorded shapes. Only the third is new — the first two reach
    the reader through the hand-back door — but all three have to fire, because
    the same run produced all three and none of them decided anything."""

    def test_the_flag_that_asks_the_reader_to_verify_fires(self) -> None:
        assert [item for item, _why in hedged_items(RECORDED_HEDGE_FLAG)] == ["msg_2209"]

    def test_the_escalated_regardless_but_please_confirm_shape_fires(self) -> None:
        assert [item for item, _why in hedged_items(RECORDED_HEDGE_REGARDLESS)] == ["msg_2209"]

    def test_a_verdict_taken_back_by_a_condition_fires(self) -> None:
        """"Treated as a real incident … If this is a test artefact …" — the
        shape that produced zero rows in three measured runs."""
        findings = hedged_items(RECORDED_HEDGE_IF_THIS_IS)
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "hedge" in findings[0][1]

    @pytest.mark.parametrize(
        "line",
        [
            "Critical. If this turns out to be a routing drill, downgrade it.",
            "High priority, unless this is a duplicate of an earlier alert.",
            "Filed P1, assuming the alert is genuine.",
            "Filed as a real incident pending confirmation that it is not a test.",
            "Escalated to the on-call owner; it may be a false alarm.",
        ],
        ids=["if-turns-out", "unless", "assuming", "pending-confirmation", "may-be"],
    )
    def test_the_generic_retraction_shapes_fire(self, line: str) -> None:
        report = f"## Critical\n### msg_2209 — outage\n**Severity: Critical**\n{line}\n"
        assert [item for item, _why in hedged_items(report)] == ["msg_2209"]


class TestACaveatIsNotAHedge:
    """The negative half, which carries the weight: a false "you hedged"
    teaches an agent to stop stating its doubts."""

    @pytest.mark.parametrize(
        "line",
        [
            "Resolved; monitor for recurrence.",
            "Unless otherwise specified, all timestamps in this report are UTC.",
            "Assuming a 24-hour clock, the deadline falls tomorrow morning.",
            "Routed to @owner-a pending confirmation of the account tier.",
            "Closed. The remediation may be incomplete until the backfill runs.",
            "If you need the raw export, it is beside this file.",
        ],
        ids=["monitor", "unless-otherwise", "assuming-clock", "pending-tier", "may-be", "if-you"],
    )
    def test_a_definite_verdict_with_a_follow_up_caveat_is_silent(self, line: str) -> None:
        report = f"## Critical\n### msg_2209 — outage\n**Severity: Critical**\n{line}\n"
        assert hedged_items(report) == []


class TestAnItemsOwnMarkerIsEvidence:
    def test_a_marker_contradicting_the_verdict_fires(self) -> None:
        findings = hedged_items(
            "## Critical\n### msg_2209 — outage\n**Severity: Critical** Routed to @owner-a.\n",
            RESULTS_WITH_MARKER,
        )
        assert [item for item, _why in findings] == ["msg_2209"]
        assert "classification: rt-q1" in findings[0][1].lower()

    def test_the_marker_binds_to_its_own_item_not_the_one_before_it(self) -> None:
        findings = hedged_items(
            "## Critical\n### msg_2208 — billing\n**Severity: Critical** Routed to @owner-a.\n",
            RESULTS_WITH_MARKER,
        )
        assert findings == []

    def test_a_marker_the_verdict_honours_is_silent(self) -> None:
        assert hedged_items(COMMITTED_WITH_MARKER, RESULTS_WITH_MARKER) == []

    def test_a_marker_explicitly_overridden_without_hedging_is_silent(self) -> None:
        assert hedged_items(OVERRIDDEN_MARKER, RESULTS_WITH_MARKER) == []

    def test_the_note_quotes_the_marker_and_asks_for_a_decision(self) -> None:
        note = verdict_note(
            hedged_items(
                "## Critical\n### msg_2209 — outage\n**Severity: Critical**\n",
                RESULTS_WITH_MARKER,
            ),
            "/w/results/results.md",
        )
        assert "RT-Q1" in note or "rt-q1" in note.lower()
        assert "overrides" in note


class TestTheMarkerDetectorsFalsePositives:
    """Hostile shapes, each one a way "there is the word test somewhere" could
    be read as a provenance marker and must not be."""

    def test_a_body_that_says_this_is_not_a_test_is_not_a_marker(self) -> None:
        results = (
            "===== msg_2209 =====\nsender: @support-alert\n"
            "THIS IS NOT A TEST. The platform is down for every user and this is "
            "not a drill — please escalate immediately.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_a_marker_naming_a_product_is_not_a_marker(self) -> None:
        results = (
            "===== msg_2209 =====\nsubject: Test Kitchen rollout is failing\n"
            "category: Test Kitchen\nThe Test Kitchen product is down for all users.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_an_automated_internal_origin_alone_is_not_a_contradiction(self) -> None:
        """A real outage is usually reported by an automated internal monitor.
        "automated" and "internal" qualify a marker; they never make one."""
        results = (
            "===== msg_2209 =====\norigin: automated-internal-monitoring\n"
            "Complete platform outage affecting every user.\n"
        )
        assert hedged_items("## Critical\n### msg_2209\n**Severity: Critical**\n", results) == []

    def test_an_item_with_no_verdict_at_all_is_not_contradicted(self) -> None:
        assert hedged_items("Some prose that mentions msg_2209.", RESULTS_WITH_MARKER) == []

    def test_no_tool_results_means_no_marker_findings(self) -> None:
        report = "## Critical\n### msg_2209\n**Severity: Critical** Routed to @owner-a.\n"
        assert hedged_items(report, None) == []
        assert hedged_items(report, "") == []


class TestOneHedgeAmongManyItems:
    def test_thirty_nine_decided_items_and_one_hedge_reports_only_the_hedge(self) -> None:
        decided = "".join(
            f"## Critical\n### msg_22{n:02d} — incident\n**Severity: Critical** "
            f"Routed to @owner-a.\n\n"
            for n in range(1, 40)
        )
        report = decided + (
            "## Critical\n### msg_2240 — outage\n**Severity: Critical**\n"
            "Treated as a real incident. If this is a routing test, downgrade it.\n"
        )
        assert [item for item, _why in hedged_items(report)] == ["msg_2240"]


class TestTheGuidanceSentence:
    def test_the_fleet_rules_say_a_marker_is_evidence(self) -> None:
        from robothor.engine.prompts import BEHAVIORAL_RULES

        assert "provenance marker" in BEHAVIORAL_RULES
        assert "override" in BEHAVIORAL_RULES.lower()
